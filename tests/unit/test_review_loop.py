"""Cobertura determinística do ciclo de correção após review rejeitado."""

from __future__ import annotations

import json
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
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA
from ai_dev_orchestrator.services.validation import GateResult


SHA_A = "a" * 40
SHA_B = "b" * 40


@dataclass
class LoopFakes:
    head: str = SHA_A
    rejected_reviews: int = 1
    wrong_session: bool = False
    events: list[str] = field(default_factory=list)
    resume_prompts: list[str] = field(default_factory=list)

    def get_issue(self, number: int) -> Issue:
        return Issue(number, "Título", "Critérios originais", "OPEN", "url", (), ())

    def list_items(self) -> tuple[ProjectItem, ...]:
        return (ProjectItem("item", "Issue", 31, "Título", "url", "acme/repo", "Ready", None, None, None, None),)

    def set_status(self, item: str, status: str) -> None:
        self.events.append(f"status:{status}")

    def create_worktree(self, repository: Path, branch: str, path: Path, base: str) -> GitWorktree:
        return GitWorktree(repository, path, branch, base)

    def execute(self, worktree: Path, prompt: str) -> CodexExecution:
        self.events.append("execute")
        return CodexExecution("sessao-original", "inicial", "", "", True)

    def resume(self, worktree: Path, session_id: str, prompt: str) -> CodexExecution:
        self.events.append(f"resume:{session_id}:{worktree}")
        self.resume_prompts.append(prompt)
        returned = "sessao-outra" if self.wrong_session else session_id
        return CodexExecution(returned, "corrigido", "", "", True)

    def validate(self, worktree: Path) -> tuple[GateResult, ...]:
        self.events.append("gates")
        return (GateResult("test", ("pytest",), True, 0, ""),)

    def commit(self, worktree: Path, issue_number: int) -> str:
        self.events.append("commit-inicial")
        return SHA_A

    def commit_correction(self, worktree: Path) -> str:
        self.events.append("commit-correcao")
        self.head = SHA_B
        return SHA_B

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.events.append(f"push:{branch}")

    def create(self, issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> PullRequest:
        self.events.append("criar-pr")
        return PullRequest(32, "https://github.com/acme/repo/pull/32", issue.title, "main", branch)

    def get_ci_snapshot(self, number: int) -> PullRequestCiSnapshot:
        self.events.append(f"ci:{self.head}")
        return PullRequestCiSnapshot(self.head, (StatusCheck("test", "COMPLETED", "SUCCESS"),))

    def get_review_data(self, number: int) -> dict:
        self.events.append(f"dossier:{self.head}")
        return {"number": 32, "url": "https://github.com/acme/repo/pull/32", "state": "OPEN",
                "baseRefName": "main", "headRefName": "feat/review-loop", "headRefOid": self.head,
                "commits": [self.head], "files": ["src/example.py"], "diff": "diff --git a/x b/x\n+x"}

    def invoke(self, prompt: str, cwd: Path, schema: dict) -> str:
        if schema == REVIEW_PLAN_SCHEMA:
            return json.dumps({key: ["x"] for key in schema["required"]})
        verdict = "REJECTED" if self.rejected_reviews > 0 else "APPROVED"
        if verdict == "REJECTED":
            self.rejected_reviews -= 1
        findings = ([{"severity": "HIGH", "title": "Corrigir", "description": "Ajuste", "path": "src/example.py", "line": 4, "criterion": "Critério"}]
                    if verdict == "REJECTED" else [])
        return json.dumps({"verdict": verdict, "findings": findings, "reviewed_head_sha": self.head, "summary": "ok"})


def pipeline(tmp_path: Path, fakes: LoopFakes, maximum: int = 3) -> RunPipeline:
    config = OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        review={"max_correction_attempts": maximum},
    )
    return RunPipeline(config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes)


def test_rejected_review_resumes_same_session_then_approves(tmp_path: Path) -> None:
    fakes = LoopFakes()
    result = pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert result.review is not None and str(result.review.verdict) == "APPROVED"
    assert (result.session_id, result.correction_attempts, result.review_attempts) == ("sessao-original", 1, 2)
    assert fakes.events.count("criar-pr") == 1
    assert fakes.events.count("gates") == 2
    assert fakes.events.count(f"ci:{SHA_A}") == fakes.events.count(f"ci:{SHA_B}") == 1
    assert any(event.startswith("resume:sessao-original:") for event in fakes.events)
    prompt = fakes.resume_prompts[0]
    for text in ("Critérios originais", SHA_A, "HIGH", "src/example.py", "Não crie Pull Request"):
        assert text in prompt


def test_limit_stops_without_a_second_resume(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=2)

    with pytest.raises(RunPipelineError, match="Limite de correções atingido.*sessão"):
        pipeline(tmp_path, fakes, maximum=1).run(31, "feat/review-loop")

    assert len(fakes.resume_prompts) == 1
    assert fakes.events.count("criar-pr") == 1


def test_different_session_from_resume_fails_before_gates_or_publication(tmp_path: Path) -> None:
    fakes = LoopFakes(wrong_session=True)

    with pytest.raises(RunPipelineError, match="sessão diferente"):
        pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert fakes.events.count("gates") == 1
    assert "commit-correcao" not in fakes.events
