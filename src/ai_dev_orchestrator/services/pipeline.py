"""Coordena a primeira execução de uma Issue sem ocultar mutações."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
from typing import Protocol

from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexExecution
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.adapters.github import (
    GitHubIssueAdapter,
    GitHubProjectAdapter,
    GitHubProjectStatusAdapter,
)
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem, is_eligible_for_execution
from ai_dev_orchestrator.domain.worktree import GitWorktree


class RunPipelineError(Exception):
    """Indica em qual etapa a execução foi interrompida."""


class IssueReader(Protocol):
    def get_issue(self, number: int) -> Issue: ...


class ProjectReader(Protocol):
    def list_items(self) -> tuple[ProjectItem, ...]: ...


class ProjectStatusWriter(Protocol):
    def set_status(self, project_item_id: str, status_name: str) -> None: ...


class WorktreeCreator(Protocol):
    def create_worktree(
        self, repository: str | Path, branch: str, worktree_path: str | Path, base_ref: str
    ) -> GitWorktree: ...


class CodexExecutor(Protocol):
    def execute(self, worktree: str | Path, prompt: str) -> CodexExecution: ...


@dataclass(frozen=True)
class RunResult:
    """Resultado imutável da execução inicial de uma Issue."""

    issue_number: int
    project_item_id: str
    branch: str
    worktree_path: Path
    base_ref: str
    session_id: str
    final_message: str
    project_status: str


def derive_worktree_path(worktrees_dir: Path, branch: str) -> Path:
    """Deriva um diretório seguro e determinístico sob a raiz configurada."""
    windows_branch = PureWindowsPath(branch)
    if not branch or Path(branch).is_absolute() or windows_branch.is_absolute():
        raise RunPipelineError("A etapa de preparar o worktree recusou uma branch com caminho absoluto")
    parts = re.split(r"[\\\\/]", branch)
    if any(part in {"", ".", ".."} for part in parts):
        raise RunPipelineError("A etapa de preparar o worktree recusou uma branch com caminho inseguro")
    directory_name = "--".join(re.sub(r"[^A-Za-z0-9._-]", "-", part) for part in parts)
    root = worktrees_dir.resolve()
    candidate = (root / directory_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RunPipelineError("A etapa de preparar o worktree gerou um caminho fora de worktrees_dir") from error
    return candidate


def build_initial_prompt(issue: Issue) -> str:
    """Monta o prompt inicial de forma determinística, sem consultar providers."""
    return (
        f"Implemente a Issue #{issue.number}: {issue.title}\n\n"
        "Body completo da Issue:\n"
        f"{issue.body}\n\n"
        "Você está executando dentro do worktree já preparado para esta Issue. "
        "Leia e respeite o AGENTS.md do repositório/worktree. Trabalhe somente no escopo "
        "desta Issue. Nesta etapa, não faça commit, push, Pull Request ou merge. "
        "Execute as validações pedidas pela própria Issue quando aplicável."
    )


class RunPipeline:
    """Executa as etapas ordenadas de preparação e início de uma sessão Codex."""

    def __init__(
        self,
        config: OrchestratorConfig,
        issue_reader: IssueReader,
        project_reader: ProjectReader,
        status_writer: ProjectStatusWriter,
        worktree_creator: WorktreeCreator,
        codex_executor: CodexExecutor,
    ) -> None:
        self.config = config
        self.issue_reader = issue_reader
        self.project_reader = project_reader
        self.status_writer = status_writer
        self.worktree_creator = worktree_creator
        self.codex_executor = codex_executor

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> RunPipeline:
        return cls(config, GitHubIssueAdapter(config), GitHubProjectAdapter(config),
                   GitHubProjectStatusAdapter(config), GitWorktreeAdapter(), CodexAdapter())

    def run(self, issue_number: int, branch: str) -> RunResult:
        if issue_number <= 0:
            raise RunPipelineError("A Issue deve ser um inteiro positivo")
        try:
            issue = self.issue_reader.get_issue(issue_number)
            item = self._find_project_item(issue_number)
            if not is_eligible_for_execution(item, self.config.github.repository_full_name,
                                             self.config.github.ready_status):
                raise RunPipelineError(
                    f"A etapa de validar elegibilidade falhou: a Issue #{issue_number} não está em "
                    f"'{self.config.github.ready_status}' no repositório configurado"
                )
            worktree_path = derive_worktree_path(self.config.workspace.worktrees_dir, branch)
        except RunPipelineError:
            raise
        except Exception as error:
            raise RunPipelineError(f"Falha antes de criar o worktree: {error}") from error
        try:
            worktree = self.worktree_creator.create_worktree(
                self.config.workspace.repository_path, branch, worktree_path,
                self.config.workspace.base_ref)
        except Exception as error:
            raise RunPipelineError(f"Falha ao criar branch e worktree: {error}") from error
        try:
            self.status_writer.set_status(item.id, self.config.github.in_progress_status)
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao alterar o Status para '{self.config.github.in_progress_status}'; "
                "worktree e branch foram preservados em "
                f"{worktree.path}: {error}") from error
        try:
            execution = self.codex_executor.execute(worktree.path, build_initial_prompt(issue))
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao executar o Codex; o Status está em '{self.config.github.in_progress_status}' "
                "e o worktree foi preservado em "
                f"{worktree.path}: {error}") from error
        return RunResult(issue.number, item.id, worktree.branch, worktree.path, worktree.base_ref,
                         execution.session_id, execution.final_message,
                         self.config.github.in_progress_status)

    def _find_project_item(self, issue_number: int) -> ProjectItem:
        repository = self.config.github.repository_full_name
        matches = [item for item in self.project_reader.list_items()
                   if item.is_issue and item.repository == repository and item.issue_number == issue_number]
        if not matches:
            raise RunPipelineError(
                f"A etapa de localizar item do Project falhou: Issue #{issue_number} não encontrada")
        if len(matches) > 1:
            raise RunPipelineError(
                f"A etapa de localizar item do Project falhou: Issue #{issue_number} é ambígua")
        return matches[0]
