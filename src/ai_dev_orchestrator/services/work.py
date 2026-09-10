"""Camada autônoma que escolhe ou retoma uma execução existente."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Protocol

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.adapters.github import (
    GitHubIssueAdapter,
    GitHubProjectAdapter,
    GitHubProjectStatusAdapter,
)
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import RunRecord
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem, is_eligible_for_execution
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunResult
from ai_dev_orchestrator.services.resume import ResumeResult, ResumeService


class WorkError(Exception):
    """O trabalho autônomo não pode prosseguir com segurança."""


class ActiveExecutionReader(Protocol):
    def list_active(self) -> tuple[RunRecord, ...]: ...
    def list_historical_candidates(self) -> tuple[RunRecord, ...]: ...
    def list_reconciliation_required(self) -> tuple[RunRecord, ...]: ...


class ProjectReader(Protocol):
    def list_items(self) -> tuple[ProjectItem, ...]: ...


class ProjectStatusWriter(Protocol):
    def set_status(self, project_item_id: str, status_name: str) -> None: ...


class IssueReader(Protocol):
    def get_issue(self, number: int) -> Issue: ...


class PipelineRunner(Protocol):
    def run(
        self, issue_number: int, branch: str, *, base_ref: str | None = None
    ) -> RunResult: ...


class ExecutionResumer(Protocol):
    def resume(self, issue_number: int) -> ResumeResult: ...


class BaseSynchronizer(Protocol):
    def prepare_remote_base(
        self, repository: object, remote_name: str, base_ref: str, branch: str
    ) -> str: ...


@dataclass(frozen=True)
class WorkResult:
    resumed: bool
    run: RunResult | None = None
    resume: ResumeResult | None = None


_PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def branch_from_title(title: str, issue_number: int | None = None) -> str:
    """Gera branch ASCII determinística sem incorporar o número da Issue."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    if issue_number is not None:
        ascii_title = re.sub(
            rf"(?<!\d){re.escape(str(issue_number))}(?!\d)", " ", ascii_title
        )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    slug = slug[:48].rstrip("-") or "trabalho"
    return f"work/{slug}"


class WorkService:
    """Retoma primeiro; na ausência de execução ativa, inicia a próxima Issue."""

    def __init__(
        self,
        config: OrchestratorConfig,
        store: ActiveExecutionReader,
        project_reader: ProjectReader,
        issue_reader: IssueReader,
        pipeline: PipelineRunner,
        resume_service: ExecutionResumer,
        base_synchronizer: BaseSynchronizer,
        status_writer: ProjectStatusWriter | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.project_reader = project_reader
        self.issue_reader = issue_reader
        self.pipeline = pipeline
        self.resume_service = resume_service
        self.base_synchronizer = base_synchronizer
        self.status_writer = status_writer

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "WorkService":
        status_writer = GitHubProjectStatusAdapter(config)
        return cls(
            config,
            SqliteExecutionStore(config.state.database_path),
            GitHubProjectAdapter(config),
            GitHubIssueAdapter(config),
            RunPipeline.from_config(config),
            ResumeService.from_config(config),
            GitWorktreeAdapter(),
            status_writer,
        )

    def work(self) -> WorkResult | None:
        """Mantém o contrato sequencial de ``orch work``."""
        orphaned = self.store.list_reconciliation_required()
        if orphaned:
            raise WorkError("Execução publicada exige reconciliação ou supersessão explícita: "
                            + ", ".join(f"#{run.issue_number} (--recover-failed ou orch supersede)" for run in orphaned))
        historical = self.store.list_historical_candidates()
        if historical:
            raise WorkError("Execução histórica com falha transitória exige reconciliação: "
                            + ", ".join(f"#{run.issue_number} (--recover-failed)" for run in historical))
        active = self.store.list_active()
        if len(active) > 1:
            issues = ", ".join(f"#{run.issue_number}" for run in active)
            raise WorkError(f"Execuções ativas ambíguas: {issues}")
        if active:
            if getattr(active[0], "phase", None) is not None and active[0].phase.value == "HUMAN_REQUIRED":
                raise WorkError(f"Execução #{active[0].issue_number} exige reconciliação ou supersessão explícita")
            return WorkResult(
                resumed=True,
                resume=self.resume_service.resume(active[0].issue_number),
            )

        return self.start_next()

    def resume_issue(self, issue_number: int) -> WorkResult:
        """Retoma somente a execução indicada pelo supervisor.

        A identidade (sessão, worktree e PR) é sempre lida do checkpoint pelo
        ResumeService; esta operação não seleciona nem cria outra Issue.
        """
        return WorkResult(resumed=True, resume=self.resume_service.resume(issue_number))

    def start_next(self, excluded_issue_numbers: frozenset[int] = frozenset()) -> WorkResult | None:
        """Inicia a próxima Issue Ready fora do conjunto já ocupado.

        O ``create`` transacional do pipeline continua sendo o claim durável:
        se outro supervisor tiver reservado a mesma Issue, a execução falha
        fechada antes de criar worktree ou iniciar Codex.
        """
        try:
            selected = self._select_issue(excluded_issue_numbers)
        except Exception as error:
            raise WorkError(f"Falha ao selecionar a próxima Issue: {error}") from error
        if selected is None:
            return None
        item, issue = selected
        branch = branch_from_title(issue.title, issue.number)
        try:
            remote_base = self.base_synchronizer.prepare_remote_base(
                self.config.workspace.repository_path,
                self.config.workspace.remote_name,
                self.config.workspace.base_ref,
                branch,
            )
        except Exception as error:
            raise WorkError(f"Falha ao sincronizar a base remota: {error}") from error
        # O pipeline relê e revalida a Issue e o item imediatamente antes da mutação.
        result = self.pipeline.run(item.issue_number or issue.number, branch, base_ref=remote_base)
        return WorkResult(resumed=False, run=result)

    def _select_issue(self, excluded_issue_numbers: frozenset[int] = frozenset()) -> tuple[ProjectItem, Issue] | None:
        items = self.project_reader.list_items()
        candidates = [
            item
            for item in items
            if is_eligible_for_execution(
                item,
                self.config.github.repository_full_name,
                self.config.github.ready_status,
            )
            and (not item.agent or item.agent.strip().casefold() == "codex")
            and item.issue_number is not None
            and item.issue_number not in excluded_issue_numbers
        ]
        candidates.sort(
            key=lambda item: (
                _PRIORITIES.get((item.priority or "").upper(), 4),
                item.issue_number or 0,
            )
        )
        for item in candidates:
            issue = self.issue_reader.get_issue(item.issue_number or 0)
            if issue.state == "OPEN":
                return item, issue
        backlog = [
            item
            for item in items
            if item.is_issue
            and item.repository == self.config.github.repository_full_name
            and item.status == "Backlog"
            and (not item.agent or item.agent.strip().casefold() == "codex")
            and item.issue_number is not None
            and item.issue_number not in excluded_issue_numbers
        ]
        backlog.sort(
            key=lambda item: (_PRIORITIES.get((item.priority or "").upper(), 4), item.issue_number or 0)
        )
        for item in backlog:
            issue = self.issue_reader.get_issue(item.issue_number or 0)
            if issue.state != "OPEN":
                continue
            if self.status_writer is None:
                raise WorkError("Promoção automática Backlog → Ready exige gravador de Status")
            self.status_writer.set_status(item.id, self.config.github.ready_status)
            return item, issue
        return None
