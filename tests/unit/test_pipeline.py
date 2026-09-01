"""Testes unitários da coordenação do primeiro fluxo ``orch run``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.codex import CodexExecution
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.services.pipeline import (
    RunPipeline,
    RunPipelineError,
    build_initial_prompt,
    derive_worktree_path,
)


def config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1,
                "ready_status": "Ready", "in_progress_status": "In Progress"},
        workspace={"repository_path": str(tmp_path / "repo"),
                   "worktrees_dir": str(tmp_path / "worktrees"), "base_ref": "origin/main"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
    )


def issue() -> Issue:
    return Issue(17, "Executar pipeline", "Body completo", "OPEN", "url", (), ())


def item(status: str = "Ready", repository: str = "acme/repo") -> ProjectItem:
    return ProjectItem("item-17", "Issue", 17, "Executar pipeline", "url", repository,
                       status, None, None, None, None)


@dataclass
class FakeIssueReader:
    value: Issue = field(default_factory=issue)
    calls: list[int] = field(default_factory=list)

    def get_issue(self, number: int) -> Issue:
        self.calls.append(number)
        return self.value


@dataclass
class FakeProjectReader:
    items: tuple[ProjectItem, ...]
    calls: int = 0

    def list_items(self) -> tuple[ProjectItem, ...]:
        self.calls += 1
        return self.items


@dataclass
class FakeStatusWriter:
    calls: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    def set_status(self, project_item_id: str, status_name: str) -> None:
        self.calls.append((project_item_id, status_name))
        if self.error:
            raise self.error


@dataclass
class FakeWorktreeCreator:
    calls: list[tuple[Path, str, Path, str]] = field(default_factory=list)
    error: Exception | None = None

    def create_worktree(self, repository: Path, branch: str, path: Path, base_ref: str) -> GitWorktree:
        self.calls.append((repository, branch, path, base_ref))
        if self.error:
            raise self.error
        return GitWorktree(repository, path, branch, base_ref)


@dataclass
class FakeCodex:
    calls: list[tuple[Path, str]] = field(default_factory=list)
    error: Exception | None = None

    def execute(self, worktree: Path, prompt: str) -> CodexExecution:
        self.calls.append((worktree, prompt))
        if self.error:
            raise self.error
        return CodexExecution("session-17", "Concluído", "", "", True)


def pipeline(tmp_path: Path, items: tuple[ProjectItem, ...] = (item(),)) -> tuple[RunPipeline, FakeStatusWriter, FakeWorktreeCreator, FakeCodex]:
    status, worktree, codex = FakeStatusWriter(), FakeWorktreeCreator(), FakeCodex()
    return RunPipeline(config(tmp_path), FakeIssueReader(), FakeProjectReader(items), status, worktree, codex), status, worktree, codex


def test_runs_in_required_order_and_returns_codex_result(tmp_path: Path) -> None:
    service, status, worktree, codex = pipeline(tmp_path)

    result = service.run(17, "feat/pipeline")

    assert worktree.calls == [(tmp_path / "repo", "feat/pipeline", tmp_path / "worktrees" / "feat--pipeline", "origin/main")]
    assert status.calls == [("item-17", "In Progress")]
    assert codex.calls[0][0] == tmp_path / "worktrees" / "feat--pipeline"
    assert (result.session_id, result.final_message, result.project_status) == ("session-17", "Concluído", "In Progress")


@pytest.mark.parametrize("items, message", [
    ((), "não encontrada"),
    ((item(), item()), "ambígua"),
    ((item("Backlog"),), "elegibilidade"),
    ((item(repository="other/repo"),), "não encontrada"),
])
def test_invalid_project_item_stops_before_mutations(tmp_path: Path, items: tuple[ProjectItem, ...], message: str) -> None:
    service, status, worktree, codex = pipeline(tmp_path, items)

    with pytest.raises(RunPipelineError, match=message):
        service.run(17, "feat/pipeline")

    assert status.calls == worktree.calls == codex.calls == []


def test_worktree_failure_does_not_change_status_or_execute_codex(tmp_path: Path) -> None:
    service, status, worktree, codex = pipeline(tmp_path)
    worktree.error = RuntimeError("git falhou")

    with pytest.raises(RunPipelineError, match="criar branch"):
        service.run(17, "feat/pipeline")

    assert status.calls == codex.calls == []


def test_status_failure_preserves_worktree_and_does_not_execute_codex(tmp_path: Path) -> None:
    service, status, worktree, codex = pipeline(tmp_path)
    status.error = RuntimeError("GitHub falhou")

    with pytest.raises(RunPipelineError, match="preservados"):
        service.run(17, "feat/pipeline")

    assert len(worktree.calls) == 1
    assert codex.calls == []


def test_codex_failure_preserves_worktree_without_cleanup(tmp_path: Path) -> None:
    service, status, worktree, codex = pipeline(tmp_path)
    codex.error = RuntimeError("Codex falhou")

    with pytest.raises(RunPipelineError, match="preservado"):
        service.run(17, "feat/pipeline")

    assert len(worktree.calls) == len(status.calls) == len(codex.calls) == 1


@pytest.mark.parametrize("branch", ["../escape", "feature/../escape", "/absolute", "C:\\absolute", "feature//name"])
def test_rejects_unsafe_worktree_paths(tmp_path: Path, branch: str) -> None:
    with pytest.raises(RunPipelineError):
        derive_worktree_path(tmp_path / "worktrees", branch)


def test_prompt_contains_issue_body_and_required_instructions() -> None:
    prompt = build_initial_prompt(issue())

    for text in ("#17", "Executar pipeline", "Body completo", "AGENTS.md", "somente no escopo", "commit, push, Pull Request ou merge", "validações"):
        assert text in prompt
