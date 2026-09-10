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
from ai_dev_orchestrator.services.merge import MergePullRequestSnapshot, MergeResult
from ai_dev_orchestrator.services.convergence import ConvergencePoller
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA
from ai_dev_orchestrator.services.validation import GateResult


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


@dataclass
class LoopFakes:
    head: str = SHA_A
    rejected_reviews: int = 1
    wrong_session: bool = False
    pre_validation_fails: bool = False
    pre_push_validation_fails: bool = False
    local_head: str = SHA_A
    resumed: bool = False
    merged: bool = False
    merge_error: Exception | None = None
    done_error: Exception | None = None
    merge_snapshot_changes: dict[str, object] = field(default_factory=dict)
    merge_calls: list[tuple[int, str]] = field(default_factory=list)
    ci_head: str | None = None
    merge_local_head: str | None = None
    correction_commits: int = 0
    events: list[str] = field(default_factory=list)
    resume_prompts: list[str] = field(default_factory=list)
    stale_pr_reads_after_correction: int = 0
    correction_was_pushed: bool = False
    reviews_completed: int = 0
    stale_merge_reads_after_merge: int = 0

    def get_issue(self, number: int) -> Issue:
        return Issue(number, "Título", "Critérios originais", "OPEN", "url", (), ())

    def list_items(self) -> tuple[ProjectItem, ...]:
        return (ProjectItem("item", "Issue", 31, "Título", "url", "acme/repo", "Ready", None, None, None, None),)

    def set_status(self, item: str, status: str) -> None:
        self.events.append(f"status:{status}")
        if status == "Done" and self.done_error is not None:
            raise self.done_error

    def create_worktree(self, repository: Path, branch: str, path: Path, base: str) -> GitWorktree:
        return GitWorktree(repository, path, branch, base)

    def execute(self, worktree: Path, prompt: str) -> CodexExecution:
        self.events.append("execute")
        return CodexExecution("sessao-original", "inicial", "", "", True)

    def resume(self, worktree: Path, session_id: str, prompt: str) -> CodexExecution:
        self.events.append(f"resume:{session_id}:{worktree}")
        self.resumed = True
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
        self.local_head = SHA_B if self.correction_commits == 0 else SHA_C
        self.correction_commits += 1
        return self.local_head

    def current_head(self, worktree: Path) -> str:
        self.events.append(f"local-head:{self.local_head}")
        return self.local_head

    def merge_state(self, worktree: Path) -> tuple[str, str]:
        self.events.append("merge-state")
        return "feat/review-loop", self.merge_local_head or self.local_head

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.events.append(f"push:{branch}")
        if self.correction_commits:
            self.head = self.local_head
            self.correction_was_pushed = True

    def create(self, issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> PullRequest:
        self.events.append("criar-pr")
        return PullRequest(32, "https://github.com/acme/repo/pull/32", issue.title, "main", branch)

    def get_ci_snapshot(self, number: int) -> PullRequestCiSnapshot:
        head = self.ci_head or self.head
        self.events.append(f"ci:{head}")
        return PullRequestCiSnapshot(head, (StatusCheck("test", "COMPLETED", "SUCCESS"),))

    def get_review_data(self, number: int) -> dict:
        self.events.append(f"dossier:{self.head}")
        remote_head = self.head
        if self.pre_validation_fails and self.resumed:
            remote_head = "c" * 40
        if self.pre_push_validation_fails and self.local_head == SHA_B:
            remote_head = "c" * 40
        return {"number": 32, "url": "https://github.com/acme/repo/pull/32", "state": "OPEN",
                "baseRefName": "main", "headRefName": "feat/review-loop", "headRefOid": remote_head,
                "commits": [remote_head], "files": ["src/example.py"], "diff": "diff --git a/x b/x\n+x"}

    def get_merge_snapshot(self, number: int) -> MergePullRequestSnapshot:
        observed_head = self.head
        if self.correction_was_pushed and self.stale_pr_reads_after_correction:
            self.stale_pr_reads_after_correction -= 1
            observed_head = SHA_A
        observed_merged = self.merged
        if self.merged and self.stale_merge_reads_after_merge:
            self.stale_merge_reads_after_merge -= 1
            observed_merged = False
        values: dict[str, object] = {
            "number": 32, "url": "https://github.com/acme/repo/pull/32",
            "state": "MERGED" if observed_merged else "OPEN", "is_draft": False,
            "base": "main", "head_branch": "feat/review-loop", "head_sha": observed_head,
            "mergeable": "MERGEABLE", "merged": observed_merged,
            "merge_commit_sha": "d" * 40 if observed_merged else "",
        }
        if self.reviews_completed:
            values.update(self.merge_snapshot_changes)
        self.events.append(f"merge-snapshot:{values['state']}")
        return MergePullRequestSnapshot(**values)  # type: ignore[arg-type]

    def merge(self, number: int, expected_head_sha: str) -> MergeResult:
        self.events.append("merge")
        self.merge_calls.append((number, expected_head_sha))
        if self.merge_error is not None:
            raise self.merge_error
        self.merged = True
        return MergeResult(expected_head_sha, "d" * 40)

    def verify_merge_commit(self, merge_commit_sha: str, merged_head_sha: str) -> None:
        self.events.append("verify-merge")

    def invoke(self, prompt: str, cwd: Path, schema: dict) -> str:
        if schema == REVIEW_PLAN_SCHEMA:
            return json.dumps({key: ["x"] for key in schema["required"]})
        verdict = "REJECTED" if self.rejected_reviews > 0 else "APPROVED"
        if verdict == "REJECTED":
            self.rejected_reviews -= 1
        findings = ([{"severity": "HIGH", "title": "Corrigir", "description": "Ajuste", "path": "src/example.py", "line": 4, "criterion": "Critério"}]
                    if verdict == "REJECTED" else [])
        self.reviews_completed += 1
        return json.dumps({"verdict": verdict, "findings": findings, "reviewed_head_sha": self.head, "summary": "ok"})


def pipeline(tmp_path: Path, fakes: LoopFakes, maximum: int = 3, auto_merge: bool = False) -> RunPipeline:
    config = OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": auto_merge},
        review={"max_correction_attempts": maximum},
    )
    convergence = ConvergencePoller(
        config.convergence, monotonic=lambda: 0, sleep=lambda _seconds: None
    )
    return RunPipeline(
        config, fakes, fakes, fakes, fakes, fakes, fakes, fakes, fakes,
        fakes, fakes, fakes, fakes, convergence=convergence,
    )


@pytest.mark.parametrize("kind", ["NETWORK_ERROR", "TERMINAL_QUOTA", "TIMEOUT"])
def test_pipeline_recovery_correction_preserves_identity_after_restart(tmp_path, kind):
    """Pipeline → SQLite → restart → resume: o mesmo PR recebe apenas um novo commit."""
    from datetime import datetime, timezone
    from ai_dev_orchestrator.domain.execution import ExecutionPhase
    from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
    from ai_dev_orchestrator.domain.recovery import (
        RecoveryObservation, RecoveryPolicy, WorktreeState, PullRequestObservation,
        PullRequestState, CiObservation, CiState, MergeObservation, MergeState,
    )
    from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
    from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
    from ai_dev_orchestrator.services.recovery_executor import RecoveryExecutor
    from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
    from ai_dev_orchestrator.services.resume import ResumeService
    from ai_dev_orchestrator.services.ci_gate import CiGate

    class InterruptedFakes(LoopFakes):
        fail = True

        def resume(self, worktree, session_id, prompt):
            if self.fail:
                self.events.append(f"interrupted:{session_id}")
                raise ProviderFailure("codex", ProviderFailureKind(kind), "diagnóstico",
                                      datetime(2026, 1, 1, tzinfo=timezone.utc),
                                      session_id=session_id, returncode=126)
            return super().resume(worktree, session_id, prompt)

    fakes = InterruptedFakes()
    initial = pipeline(tmp_path, fakes)
    initial.config.supervisor.retry_without_reset_seconds = 60
    database = tmp_path / "state.db"
    initial.execution_store = SqliteExecutionStore(database)
    with pytest.raises(RunPipelineError, match="preservada"):
        initial.run(31, "feat/review-loop")
    waiting = initial.execution_store.get_latest_for_issue(31)
    assert waiting.phase == (ExecutionPhase.WAITING_CODEX_QUOTA if kind == "TERMINAL_QUOTA" else ExecutionPhase.WAITING_PROVIDER)
    assert waiting.provider_resume_phase == "CODEX_RUNNING"
    assert waiting.codex_session_id == "sessao-original"
    assert waiting.correction_attempts == 1
    assert waiting.reviewed_head_sha == waiting.ci_head_sha == waiting.current_head_sha == SHA_A
    assert waiting.review_verdict == "REJECTED"
    assert waiting.pull_request_number == 32 and waiting.project_status == "AI Review"
    prior_findings = initial.execution_store.review_findings(waiting.id, SHA_A)
    assert prior_findings

    store = SqliteExecutionStore(database)
    fakes.fail = False
    class Observer:
        def observe(self, run):
            return RecoveryObservation(
                WorktreeState.CONVERGENT, local_head_sha=fakes.local_head,
                local_head_parent_sha=SHA_A, has_worktree_changes=fakes.local_head == SHA_A,
                remote_head_sha=fakes.head,
                pull_requests=(PullRequestObservation(32, waiting.pull_request_url, "acme/repo", "main",
                                                      waiting.branch, fakes.head, PullRequestState.OPEN),),
                ci=CiObservation(CiState.SUCCESS, fakes.head),
                findings_head_sha=run.reviewed_head_sha if store.review_findings(run.id, run.reviewed_head_sha) else None,
                merge=MergeObservation(MergeState.OPEN),
            )

    effects = RecoveryEffects(initial.config, projects=fakes)
    effects.codex = fakes
    effects.validation = fakes
    effects.publication = fakes
    effects.issues = fakes
    effects.pull_requests = fakes
    effects.reviewer = fakes
    effects._wait_ci_result = lambda run: CiGate(fakes, initial.config.ci).wait(run.pull_request_number, run.current_head_sha)
    policy = RecoveryPolicy("acme/repo", "main", False, 3)
    result = ResumeService(store, Observer(), RecoveryPlanner(policy), RecoveryExecutor(policy, store, effects)).resume(
        31, retry_provider=kind == "TERMINAL_QUOTA",
    )
    final = store.get(waiting.id)
    assert result.phase == "APPROVED_AWAITING_ACTION"
    for identity_field in ("id", "issue_number", "branch", "worktree_path", "base_ref", "codex_session_id",
                  "pull_request_number", "pull_request_url", "project_item_id", "project_status"):
        assert getattr(final, identity_field) == getattr(waiting, identity_field)
    assert final.correction_attempts == 1 and final.provider_retry_attempts == 0
    assert final.current_head_sha == final.ci_head_sha == final.reviewed_head_sha == SHA_B
    assert final.review_verdict == "APPROVED"
    assert store.review_findings(final.id, SHA_A) == prior_findings
    assert not store.review_findings(final.id, SHA_B)
    assert len(store.list_history()) == 1
    assert fakes.events.count("execute") == fakes.events.count("criar-pr") == fakes.correction_commits == 1
    assert len(fakes.resume_prompts) == 1 and "Ajuste" in fakes.resume_prompts[0]
    assert not fakes.merge_calls


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
    for text in ("Critérios originais", SHA_A, "HIGH", "src/example.py", "Não crie Pull Request", "Não faça commit, push ou merge"):
        assert text in prompt


def test_pipeline_forwards_custom_blocking_severities_to_both_stages(tmp_path: Path) -> None:
    prompts = []

    class PolicyFakes(LoopFakes):
        def invoke(self, prompt, cwd, schema):
            prompts.append(prompt)
            return super().invoke(prompt, cwd, schema)

    fakes = PolicyFakes(rejected_reviews=0)
    service = pipeline(tmp_path, fakes)
    service.config.review.blocking_severities = ("LOW",)
    service.run(31, "feat/review-loop")
    assert len(prompts) == 2
    for prompt in prompts:
        authority = prompt.split("</POLITICA_AUTORITATIVA>")[0]
        assert "Severidades bloqueantes configuradas: LOW." in authority


def test_correction_waits_for_old_pr_head_and_keeps_same_session_and_pr(
    tmp_path: Path,
) -> None:
    fakes = LoopFakes(stale_pr_reads_after_correction=2)

    result = pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert result.session_id == "sessao-original"
    assert result.pull_request_number == 32
    assert result.final_reviewed_head_sha == SHA_B
    assert fakes.events.count("criar-pr") == 1
    assert fakes.events.count("push:feat/review-loop") == 2
    assert fakes.events.count("merge-snapshot:OPEN") == 4


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


def test_prevalidation_failure_blocks_correction_commit_and_push(tmp_path: Path) -> None:
    fakes = LoopFakes(pre_validation_fails=True)

    with pytest.raises(RunPipelineError, match="Pull Request existente divergiu"):
        pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert "commit-correcao" not in fakes.events
    assert fakes.events.count("push:feat/review-loop") == 1


def test_changed_local_head_blocks_correction_publication(tmp_path: Path) -> None:
    fakes = LoopFakes(local_head="c" * 40)

    with pytest.raises(RunPipelineError, match="HEAD local divergiu"):
        pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert "commit-correcao" not in fakes.events
    assert fakes.events.count("push:feat/review-loop") == 1


def test_pre_push_validation_blocks_remote_publication(tmp_path: Path) -> None:
    fakes = LoopFakes(pre_push_validation_fails=True)

    with pytest.raises(RunPipelineError, match="Pull Request existente divergiu"):
        pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert "commit-correcao" in fakes.events
    assert fakes.events.count("push:feat/review-loop") == 1


def test_auto_merge_disabled_never_calls_merger_or_writes_done(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0)

    result = pipeline(tmp_path, fakes).run(31, "feat/review-loop")

    assert result.merged is False
    assert fakes.merge_calls == []
    assert "status:Done" not in fakes.events


def test_approved_review_merges_exact_reviewed_head_then_writes_done(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0)

    result = pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == [(32, SHA_A)]
    assert result.merged and result.merge_commit_sha == "d" * 40
    assert fakes.events.index("merge") < fakes.events.index("status:Done")


def test_merge_is_mutated_once_while_confirmation_still_looks_open(
    tmp_path: Path,
) -> None:
    fakes = LoopFakes(rejected_reviews=0, stale_merge_reads_after_merge=2)

    result = pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert result.merged is True
    assert fakes.merge_calls == [(32, SHA_A)]
    assert fakes.events.count("merge") == 1


def test_merge_failure_never_writes_done(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0, merge_error=RuntimeError("GitHub falhou"))

    with pytest.raises(RunPipelineError, match="nenhum Status Done"):
        pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert "status:Done" not in fakes.events


def test_done_failure_reports_that_pr_is_already_merged(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0, done_error=RuntimeError("Project indisponível"))

    with pytest.raises(RunPipelineError, match="já foi merged"):
        pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == [(32, SHA_A)]
    assert fakes.events[-1] == "status:Done"


@pytest.mark.parametrize(("rejections", "expected_head"), [(1, SHA_B), (2, SHA_C)])
def test_review_corrections_merge_only_final_head(
    tmp_path: Path, rejections: int, expected_head: str,
) -> None:
    fakes = LoopFakes(rejected_reviews=rejections)

    pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == [(32, expected_head)]
    assert fakes.events.index("merge") > max(
        index for index, event in enumerate(fakes.events) if event == "commit-correcao"
    )


@pytest.mark.parametrize("changes", [
    {"head_sha": "c" * 40}, {"base": "release"}, {"head_branch": "other"},
    {"state": "CLOSED"}, {"is_draft": True}, {"mergeable": "CONFLICTING"},
])
def test_final_pr_divergence_refuses_merge(tmp_path: Path, changes: dict[str, object]) -> None:
    fakes = LoopFakes(rejected_reviews=0, merge_snapshot_changes=changes)

    with pytest.raises(RunPipelineError, match="Auto-merge recusado"):
        pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == []
    assert "status:Done" not in fakes.events


def test_divergent_ci_head_prevents_any_merge(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0, ci_head=SHA_B)

    with pytest.raises(RunPipelineError, match="Falha no gate de CI"):
        pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == []


def test_divergent_local_head_prevents_any_merge(tmp_path: Path) -> None:
    fakes = LoopFakes(rejected_reviews=0, merge_local_head=SHA_B)

    with pytest.raises(RunPipelineError, match="Auto-merge recusado"):
        pipeline(tmp_path, fakes, auto_merge=True).run(31, "feat/review-loop")

    assert fakes.merge_calls == []
