"""Cobertura da coordenação completa de publicação com collaborators falsos."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.codex import CodexExecution
from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.ci import PullRequestCiSnapshot, StatusCheck
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunPipelineError
from ai_dev_orchestrator.services.validation import GateResult
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA
import json


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
        return "a" * 40

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.events.append(f"push:{remote}:{branch}")
        if self.failure == "push":
            raise RuntimeError("push falhou")

    def create(self, issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> PullRequest:
        self.events.append("pr")
        if self.failure == "pr":
            raise RuntimeError("pr falhou")
        return PullRequest(20, "https://github.com/acme/repo/pull/20", issue.title, "release", branch)


def service(tmp_path: Path, fakes: Fakes, ci_reader: object | None = None) -> RunPipeline:
    config = OrchestratorConfig(github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready", "pull_request_base": "release"}, execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False}, workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "origin/main", "remote_name": "upstream"})
    return RunPipeline(config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, ci_reader)


def test_full_flow_orders_effects_and_returns_publication_result(tmp_path: Path) -> None:
    fakes = Fakes()
    result = service(tmp_path, fakes).run(19, "feat/publicar")
    assert fakes.events == ["status:item-correto:In Progress", "codex", "gates", "commit", "push:upstream:feat/publicar", "pr", "status:item-correto:AI Review"]
    assert (result.gates, result.commit_sha, result.remote_name, result.pull_request_number, result.pull_request_url, result.pull_request_base, result.project_status) == ((GateResult("ruff", ("uv",), True, 0, ""),), "a" * 40, "upstream", 20, "https://github.com/acme/repo/pull/20", "release", "AI Review")


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


@dataclass
class CiReader:
    events: list[str]
    failure: bool = False
    head_sha: str = "a" * 40

    def get_ci_snapshot(self, number: int) -> PullRequestCiSnapshot:
        self.events.append(f"ci:{number}")
        conclusion = "FAILURE" if self.failure else "SUCCESS"
        return PullRequestCiSnapshot(self.head_sha, (StatusCheck("test", "COMPLETED", conclusion),))


def test_ci_starts_only_after_ai_review_and_success_is_returned(tmp_path: Path) -> None:
    fakes = Fakes()
    result = service(tmp_path, fakes, CiReader(fakes.events)).run(19, "feat/publicar")
    assert fakes.events[-2:] == ["status:item-correto:AI Review", "ci:20"]
    assert result.pull_request_head_sha == "a" * 40
    assert str(result.ci_status) == "SUCCESS"


def test_ci_failure_preserves_publication_context_and_does_not_run_future_step(tmp_path: Path) -> None:
    fakes = Fakes()
    with pytest.raises(RunPipelineError, match=r"Issue #19.*#20.*pull/20.*AI Review.*a{40}"):
        service(tmp_path, fakes, CiReader(fakes.events, failure=True)).run(19, "feat/publicar")
    assert fakes.events[-2:] == ["status:item-correto:AI Review", "ci:20"]


def test_ci_rejects_a_head_that_already_diverged_from_the_published_commit(tmp_path: Path) -> None:
    fakes = Fakes()
    reader = CiReader(fakes.events, head_sha="b" * 40)
    with pytest.raises(RunPipelineError, match=r"AI Review.*a{40}.*b{40}"):
        service(tmp_path, fakes, reader).run(19, "feat/publicar")
    assert fakes.events[-2:] == ["status:item-correto:AI Review", "ci:20"]


def test_reviewer_runs_after_ci_with_two_fresh_worktree_invocations(tmp_path: Path) -> None:
    fakes = Fakes()

    class ReviewReader:
        def get_review_data(self, number: int):
            fakes.events.append("dossier")
            return {"number": 20, "url": "https://github.com/acme/repo/pull/20", "baseRefName": "release", "headRefName": "feat/publicar", "headRefOid": "a" * 40, "commits": ["a" * 40], "files": ["src/config.py"], "diff": "diff --git a/src/config.py b/src/config.py\n+x"}

    class Reviewer:
        def __init__(self): self.calls = []
        def invoke(self, prompt: str, cwd: Path, schema: dict):
            self.calls.append((cwd, schema))
            fakes.events.append("reviewer")
            if schema == REVIEW_PLAN_SCHEMA:
                return json.dumps({key: ["x"] for key in schema["required"]})
            return json.dumps({"verdict": "APPROVED", "findings": [], "reviewed_head_sha": "a" * 40, "summary": "ok"})

    reviewer = Reviewer()
    config = OrchestratorConfig(github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready", "pull_request_base": "release"}, execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False}, workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "origin/main", "remote_name": "upstream"})
    pipeline = RunPipeline(config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, CiReader(fakes.events), ReviewReader(), reviewer)
    result = pipeline.run(19, "feat/publicar")
    assert result.review is not None and str(result.review.verdict) == "APPROVED"
    assert [schema for _, schema in reviewer.calls] != [] and len(reviewer.calls) == 2
    assert all(cwd == result.worktree_path for cwd, _ in reviewer.calls)


def test_reviewer_rejects_head_changed_after_final_invocation(tmp_path: Path) -> None:
    fakes = Fakes()

    class ChangingReader:
        calls = 0
        def get_review_data(self, number: int):
            self.calls += 1
            sha = "a" * 40 if self.calls < 3 else "b" * 40
            return {"number": 20, "url": "u", "baseRefName": "release", "headRefName": "feat/publicar", "headRefOid": sha, "commits": ["a" * 40], "files": ["src/config.py"], "diff": "diff --git a/src/config.py b/src/config.py\n+x"}

    class Reviewer:
        def invoke(self, prompt: str, cwd: Path, schema: dict):
            if schema == REVIEW_PLAN_SCHEMA:
                return json.dumps({key: ["x"] for key in schema["required"]})
            return json.dumps({"verdict": "APPROVED", "findings": [], "reviewed_head_sha": "a" * 40, "summary": "ok"})

    config = OrchestratorConfig(github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready", "pull_request_base": "release"}, execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False}, workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "origin/main", "remote_name": "upstream"})
    pipeline = RunPipeline(config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, CiReader(fakes.events), ChangingReader(), Reviewer())
    with pytest.raises(RunPipelineError, match="HEAD"):
        pipeline.run(19, "feat/publicar")
