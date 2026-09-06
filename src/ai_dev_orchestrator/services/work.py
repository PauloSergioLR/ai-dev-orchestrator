"""Camada autônoma que escolhe ou retoma uma execução existente."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata
from typing import Protocol

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.adapters.github import GitHubIssueAdapter, GitHubProjectAdapter
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


class ProjectReader(Protocol):
    def list_items(self) -> tuple[ProjectItem, ...]: ...


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


def _parallel_branch_from_title(title: str, issue_number: int) -> str:
    """Evita colisão de worktree para títulos iguais sem expor o número da Issue."""
    suffix = sha256(str(issue_number).encode()).hexdigest()[:8]
    return f"{branch_from_title(title, issue_number)}-{suffix}"


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
    ) -> None:
        self.config = config
        self.store = store
        self.project_reader = project_reader
        self.issue_reader = issue_reader
        self.pipeline = pipeline
        self.resume_service = resume_service
        self.base_synchronizer = base_synchronizer

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "WorkService":
        return cls(
            config,
            SqliteExecutionStore(config.state.database_path),
            GitHubProjectAdapter(config),
            GitHubIssueAdapter(config),
            RunPipeline.from_config(config),
            ResumeService.from_config(config),
            GitWorktreeAdapter(),
        )

    def work(self) -> WorkResult | None:
        active = self.store.list_active()
        if len(active) > 1:
            issues = ", ".join(f"#{run.issue_number}" for run in active)
            raise WorkError(f"Execuções ativas ambíguas: {issues}")
        if active:
            return WorkResult(
                resumed=True,
                resume=self.resume_service.resume(active[0].issue_number),
            )

        try:
            selected = self._select_issue()
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

    def work_issue(self, issue_number: int) -> WorkResult | None:
        """Processa somente uma Issue, sem assumir exclusividade global.

        O supervisor usa esta entrada para manter as identidades das execuções
        separadas. A criação do registro no SQLite continua sendo a barreira
        durável contra duas execuções da mesma Issue.
        """
        active = tuple(
            run for run in self.store.list_active() if run.issue_number == issue_number
        )
        if len(active) > 1:
            raise WorkError(f"Execuções ativas ambíguas para a Issue #{issue_number}")
        if active:
            return WorkResult(
                resumed=True, resume=self.resume_service.resume(issue_number)
            )

        try:
            selected = self._select_issue(issue_number)
        except Exception as error:
            raise WorkError(f"Falha ao selecionar a Issue #{issue_number}: {error}") from error
        if selected is None:
            return None
        item, issue = selected
        branch = _parallel_branch_from_title(issue.title, issue.number)
        try:
            remote_base = self.base_synchronizer.prepare_remote_base(
                self.config.workspace.repository_path,
                self.config.workspace.remote_name,
                self.config.workspace.base_ref,
                branch,
            )
        except Exception as error:
            raise WorkError(f"Falha ao sincronizar a base remota: {error}") from error
        return WorkResult(
            resumed=False,
            run=self.pipeline.run(item.issue_number or issue.number, branch, base_ref=remote_base),
        )

    def eligible_issue_numbers(self, excluded: set[int] | None = None) -> tuple[int, ...]:
        """Retorna Issues Ready em ordem estável para o scheduler."""
        excluded = excluded or set()
        return tuple(
            item.issue_number
            for item, issue in self._eligible_issues()
            if item.issue_number is not None and item.issue_number not in excluded
        )

    def _select_issue(self, only_issue: int | None = None) -> tuple[ProjectItem, Issue] | None:
        for item in self._candidate_items():
            if only_issue is not None and item.issue_number != only_issue:
                continue
            issue = self.issue_reader.get_issue(item.issue_number or 0)
            if issue.state == "OPEN":
                return item, issue
        return None

    def _eligible_issues(self) -> tuple[tuple[ProjectItem, Issue], ...]:
        eligible: list[tuple[ProjectItem, Issue]] = []
        for item in self._candidate_items():
            issue = self.issue_reader.get_issue(item.issue_number or 0)
            if issue.state == "OPEN":
                eligible.append((item, issue))
        return tuple(eligible)

    def _candidate_items(self) -> list[ProjectItem]:
        candidates = [
            item
            for item in self.project_reader.list_items()
            if is_eligible_for_execution(
                item,
                self.config.github.repository_full_name,
                self.config.github.ready_status,
            )
            and (not item.agent or item.agent.strip().casefold() == "codex")
            and item.issue_number is not None
        ]
        candidates.sort(
            key=lambda item: (
                _PRIORITIES.get((item.priority or "").upper(), 4),
                item.issue_number or 0,
            )
        )
        return candidates
