"""Remoção conservadora de artefatos que pertencem a uma execução concluída."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.infrastructure.redaction import sanitize_diagnostic
from ai_dev_orchestrator.infrastructure.ownership import OwnershipError


class CleanupError(Exception):
    """Falha local que deve permanecer uma pendência auditável."""


class GitCleanup(Protocol):
    def worktree_is_clean(self, repository: str | Path, worktree_path: str | Path) -> bool: ...
    def remove_worktree(self, repository: str | Path, worktree_path: str | Path) -> None: ...
    def delete_local_branch(self, repository: str | Path, branch: str, expected_sha: str) -> None: ...
    def delete_remote_branch(self, repository: str | Path, remote_name: str, branch: str, expected_sha: str) -> None: ...
    def local_branch_head(self, repository: str | Path, branch: str) -> str | None: ...
    def verify_cleanup_worktree(self, repository: str | Path, worktree_path: str | Path, branch: str, expected_sha: str) -> None: ...
    def local_branch_exists(self, repository: str | Path, branch: str) -> bool: ...
    def remote_branch_exists(self, repository: str | Path, remote_name: str, branch: str) -> bool: ...
    def worktree_is_registered(self, repository: str | Path, worktree_path: str | Path) -> bool: ...
    def remove_empty_orphan_directory(self, worktree_path: str | Path, allowed_root: str | Path) -> None: ...
    def quarantine_orphan_directory(
        self, worktree_path: str | Path, allowed_root: str | Path,
        execution_id: str,
    ) -> Path: ...


@dataclass(frozen=True)
class CleanupResult:
    execution_id: str
    status: str
    detail: str


class CleanupService:
    """Executa somente operações reversíveis ou que o Git recusa se inseguras."""

    def __init__(self, config: OrchestratorConfig, store: SqliteExecutionStore, git: GitCleanup) -> None:
        self.config, self.store, self.git = config, store, git

    def cleanup(
        self, execution_id: str, *, quarantine_orphan: bool = False
    ) -> CleanupResult:
        run = self.store.get(execution_id)
        try:
            with self.store.ownership(run.issue_number):
                return self._cleanup_owned(execution_id, quarantine_orphan=quarantine_orphan)
        except OwnershipError as error:
            raise CleanupError("Cleanup recusado: execução sob controle de outra operação") from error

    def _cleanup_owned(self, execution_id: str, *, quarantine_orphan: bool) -> CleanupResult:
        run = self.store.get(execution_id)
        if run.phase not in {ExecutionPhase.COMPLETED, ExecutionPhase.SUPERSEDED}:
            return self._record(
                run, "PRESERVED",
                "Execução ativa; artefatos preservados até conclusão ou supersessão",
            )
        if run.cleanup_status == "DONE":
            return CleanupResult(run.id, "DONE", "Cleanup já concluído")
        if not run.branch or not run.worktree_path:
            return self._record(run, "PRESERVED", "Identidade persistida de branch ou worktree incompleta")
        if run.repository_identity != self.config.github.repository_full_name:
            return self._record(run, "PRESERVED", "Identidade do repositório ausente ou divergente; preservado")
        protected = {
            self.config.workspace.base_branch,
            self.config.github.pull_request_target,
            *self.config.github.protected_branches,
        }
        if run.branch in protected:
            return self._record(run, "PRESERVED", "Branch base, destino ou protegida nunca é removida")
        try:
            path = GitWorktreeAdapter.validate_cleanup_path(
                run.worktree_path, self.config.workspace.worktrees_dir,
            )
            expected_sha = run.current_head_sha or run.base_sha
            local_sha = self.git.local_branch_head(self.config.workspace.repository_path, run.branch)
            if local_sha is not None and (not expected_sha or local_sha != expected_sha):
                return self._record(run, "PENDING", "HEAD atual da branch diverge ou não foi comprovado; preservado")
            if path.exists():
                is_registered = self.git.worktree_is_registered(self.config.workspace.repository_path, path)
                if is_registered:
                    if not expected_sha:
                        return self._record(run, "PENDING", "SHA da execução ausente; worktree preservado")
                    self.git.verify_cleanup_worktree(
                        self.config.workspace.repository_path, path, run.branch, expected_sha,
                    )
                    if not self.git.worktree_is_clean(
                        self.config.workspace.repository_path, path
                    ):
                        return self._record(
                            run, "PENDING",
                            "Worktree contém alterações não commitadas; preservado",
                        )
                    self.git.remove_worktree(
                        self.config.workspace.repository_path, path
                    )
                else:
                    remover = getattr(self.git, "remove_empty_orphan_directory", None)
                    if remover is None:
                        return self._record(
                            run, "PENDING",
                            "Diretório não registrado pelo Git; preservado por falta de prova",
                        )
                    try:
                        remover(path, self.config.workspace.worktrees_dir)
                    except Exception as error:
                        if not quarantine_orphan:
                            return self._record(
                                run,
                                "PENDING",
                                "Diretório órfão não vazio foi preservado; use "
                                "--quarantine-orphan para movê-lo sem apagar: "
                                f"{error}",
                            )
                        quarantine = getattr(
                            self.git, "quarantine_orphan_directory", None
                        )
                        if quarantine is None:
                            return self._record(
                                run, "PENDING",
                                "Adapter não oferece quarentena segura; preservado",
                            )
                        target = quarantine(
                            path, self.config.workspace.worktrees_dir, run.id
                        )
                        actions = [f"diretório órfão movido para {target}"]
                    else:
                        actions = ["diretório órfão vazio removido"]
                if is_registered:
                    actions = ["worktree Git limpo removido"]
            else:
                actions = ["caminho de worktree já ausente"]
            if self.config.cleanup.remove_local_branch:
                if self.git.local_branch_exists(self.config.workspace.repository_path, run.branch):
                    self.git.delete_local_branch(self.config.workspace.repository_path, run.branch, expected_sha)
                    actions.append("branch local removida")
            if self.config.cleanup.remove_remote_branch:
                if not self._remote_removal_is_proven(run):
                    return self._record(run, "PENDING", "Branch remota preservada: merge do HEAD esperado não foi comprovado")
                self.git.verify_remote_identity(
                    self.config.workspace.repository_path, self.config.workspace.remote_name,
                    self.config.github.repository_full_name,
                )
                if self.git.remote_branch_exists(self.config.workspace.repository_path, self.config.workspace.remote_name, run.branch):
                    self.git.delete_remote_branch(
                        self.config.workspace.repository_path,
                        self.config.workspace.remote_name,
                        run.branch,
                        run.merged_head_sha,
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
        detail = sanitize_diagnostic(detail)
        # Checkpoint preserva a fase terminal e deixa a tentativa vinculada ao execution_id.
        self.store.checkpoint(
            run.id,
            summary=f"Cleanup {status}: {detail}",
            cleanup_status=status,
            cleanup_detail=detail,
        )
        return CleanupResult(run.id, status, detail)
