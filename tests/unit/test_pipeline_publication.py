"""Cobertura da coordenação completa de publicação com collaborators falsos."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.codex import CodexExecution
from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunPipelineError
from ai_dev_orchestrator.services.validation import GateResult


@dataclass
class Fakes:
    events: list[str] = field(default_factory=list)
    failure: str | None = None

    def get_issue(self, number: int) -> Issue:
        return Issue(number, "Título original", "", "OPEN", "url", (), ())

    def list_items(self) -> tuple[ProjectItem, ...]:
        return (ProjectItem("item-correto", "Issue", 19, "Título original", "url", "acme/repo", "Ready", None, None, None, None),)

    def set_status(self, item_id: str, status: str) -> None:
        self.events.append(f"status:{item_id}:{status}")
        if self.failure == "review" and status == "AI Review":
            raise RuntimeError("status falhou")

    def create_worktree(self, repository: Path, branch: str, path: Path, base: str) -> GitWorktree:
        return GitWorktree(repository, path, branch, base)

    def execute(self, worktree: Path, prompt: str) -> CodexExecution:
        self.events.append("codex")
        return CodexExecution("sessão", "ok", "", "", True)

    def validate(self, worktree: Path) -> tuple[GateResult, ...]:
        self.events.append("gates")
        if self.failure == "gates":
            raise RuntimeError("ruff falhou")
        return (GateResult("ruff", ("uv",), True, 0, ""),)

    def commit(self, worktree: Path, number: int) -> str:
        self.events.append("commit")
        if self.failure == "commit":
            raise RuntimeError("commit falhou")
        return "sha-19"

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.events.append(f"push:{remote}:{branch}")
        if self.failure == "push":
            raise RuntimeError("push falhou")

    def create(self, issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> PullRequest:
        self.events.append("pr")
        if self.failure == "pr":
            raise RuntimeError("pr falhou")
        return PullRequest(20, "https://github.com/acme/repo/pull/20", issue.title, "release", branch)


def service(tmp_path: Path, fakes: Fakes) -> RunPipeline:
    config = OrchestratorConfig(github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready", "pull_request_base": "release"}, execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False}, workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "origin/main", "remote_name": "upstream"})
    return RunPipeline(config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes)


def test_full_flow_orders_effects_and_returns_publication_result(tmp_path: Path) -> None:
    fakes = Fakes()
    result = service(tmp_path, fakes).run(19, "feat/publicar")
    assert fakes.events == ["status:item-correto:In Progress", "codex", "gates", "commit", "push:upstream:feat/publicar", "pr", "status:item-correto:AI Review"]
    assert (result.gates, result.commit_sha, result.remote_name, result.pull_request_number, result.pull_request_url, result.pull_request_base, result.project_status) == ((GateResult("ruff", ("uv",), True, 0, ""),), "sha-19", "upstream", 20, "https://github.com/acme/repo/pull/20", "release", "AI Review")


@pytest.mark.parametrize("failure, forbidden", [("gates", ("commit", "push", "pr", "AI Review")), ("commit", ("push", "pr", "AI Review")), ("push", ("pr", "AI Review")), ("pr", ("AI Review",))])
def test_failures_stop_following_publication_effects(tmp_path: Path, failure: str, forbidden: tuple[str, ...]) -> None:
    fakes = Fakes(failure=failure)
    with pytest.raises(RunPipelineError):
        service(tmp_path, fakes).run(19, "feat/publicar")
    assert all(value not in event for value in forbidden for event in fakes.events)


def test_ai_review_failure_reports_existing_pr_without_rollback(tmp_path: Path) -> None:
    fakes = Fakes(failure="review")
    with pytest.raises(RunPipelineError, match=r"#20.*pull/20"):
        service(tmp_path, fakes).run(19, "feat/publicar")
    assert fakes.events[-1] == "status:item-correto:AI Review"
