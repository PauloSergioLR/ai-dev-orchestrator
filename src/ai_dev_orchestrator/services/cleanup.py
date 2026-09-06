"""Remoção conservadora de artefatos que pertencem a uma execução concluída."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


class CleanupError(Exception):
    """Falha local que deve permanecer uma pendência auditável."""


class GitCleanup(Protocol):
    def worktree_is_clean(self, repository: str | Path, worktree_path: str | Path) -> bool: ...
    def remove_worktree(self, repository: str | Path, worktree_path: str | Path) -> None: ...
    def delete_local_branch(self, repository: str | Path, branch: str) -> None: ...
    def delete_remote_branch(self, repository: str | Path, remote_name: str, branch: str) -> None: ...
    def local_branch_exists(self, repository: str | Path, branch: str) -> bool: ...
    def remote_branch_exists(self, repository: str | Path, remote_name: str, branch: str) -> bool: ...


@dataclass(frozen=True)
class CleanupResult:
    execution_id: str
    status: str
    detail: str


class CleanupService:
    """Executa somente operações reversíveis ou que o Git recusa se inseguras."""

    def __init__(self, config: OrchestratorConfig, store: SqliteExecutionStore, git: GitCleanup) -> None:
        self.config, self.store, self.git = config, store, git

    def cleanup(self, execution_id: str) -> CleanupResult:
        run = self.store.get(execution_id)
        if run.phase is not ExecutionPhase.COMPLETED:
            return self._record(run, "PRESERVED", "Execução não concluída; artefatos preservados")
        if run.cleanup_status == "DONE":
            return CleanupResult(run.id, "DONE", "Cleanup já concluído")
        if not run.branch or not run.worktree_path:
            return self._record(run, "PRESERVED", "Identidade persistida de branch ou worktree incompleta")
        protected = {
            self.config.workspace.base_branch,
            self.config.github.pull_request_target,
            *self.config.github.protected_branches,
        }
        if run.branch in protected:
            return self._record(run, "PRESERVED", "Branch base, destino ou protegida nunca é removida")
        try:
            path = Path(run.worktree_path)
            if path.exists():
                if not self.git.worktree_is_clean(self.config.workspace.repository_path, path):
                    return self._record(run, "PENDING", "Worktree contém alterações não commitadas; preservado")
                self.git.remove_worktree(self.config.workspace.repository_path, path)
            actions = ["worktree removido"]
            if self.config.cleanup.remove_local_branch:
                if self.git.local_branch_exists(self.config.workspace.repository_path, run.branch):
                    self.git.delete_local_branch(self.config.workspace.repository_path, run.branch)
                    actions.append("branch local removida")
            if self.config.cleanup.remove_remote_branch:
                if not self._remote_removal_is_proven(run):
                    return self._record(run, "PENDING", "Branch remota preservada: merge do HEAD esperado não foi comprovado")
                if self.git.remote_branch_exists(self.config.workspace.repository_path, self.config.workspace.remote_name, run.branch):
                    self.git.delete_remote_branch(
                        self.config.workspace.repository_path,
                        self.config.workspace.remote_name,
                        run.branch,
                    )
                    actions.append("branch remota removida")
        except Exception as error:
            return self._record(run, "PENDING", f"Falha no cleanup: {error}")
        return self._record(run, "DONE", "; ".join(actions))

    @staticmethod
    def _remote_removal_is_proven(run: RunRecord) -> bool:
        return bool(
            run.pull_request_number
            and run.merge_commit_sha
            and run.merged_head_sha
            and run.current_head_sha
            and run.current_head_sha == run.merged_head_sha
        )

    def _record(self, run: RunRecord, status: str, detail: str) -> CleanupResult:
        # Checkpoint preserva a fase terminal e deixa a tentativa vinculada ao execution_id.
        self.store.checkpoint(
            run.id,
            summary=f"Cleanup {status}: {detail}",
            cleanup_status=status,
            cleanup_detail=detail,
        )
        return CleanupResult(run.id, status, detail)
