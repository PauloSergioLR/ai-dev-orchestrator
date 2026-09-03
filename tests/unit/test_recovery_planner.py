"""Matriz de decisões puras do planejador de recovery."""

from datetime import datetime

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, PullRequestObservation,
    RecoveryAction, RecoveryObservation, ReviewObservation, ReviewState, WorktreeState,
)
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner

HEAD = "a" * 40
OTHER = "b" * 40
THIRD = "c" * 40


def run(phase: ExecutionPhase, **changes: object) -> RunRecord:
    now = datetime(2026, 1, 1)
    values: dict[str, object] = dict(id="run", issue_number=37, phase=phase, created_at=now, updated_at=now, current_head_sha=HEAD)
    values.update(changes)
    return RunRecord(**values)  # type: ignore[arg-type]


def observed(**changes: object) -> RecoveryObservation:
    values: dict[str, object] = dict(worktree_state=WorktreeState.CONVERGENT, local_head_sha=HEAD)
    values.update(changes)
    return RecoveryObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("phase", "snapshot", "action"), [
    (ExecutionPhase.COMMIT_PENDING, observed(has_local_changes=True), RecoveryAction.CREATE_COMMIT),
    (ExecutionPhase.PUSH_PENDING, observed(remote_head_sha=None), RecoveryAction.PUSH_BRANCH),
    (ExecutionPhase.PR_PENDING, observed(remote_head_sha=HEAD), RecoveryAction.CREATE_PULL_REQUEST),
])
def test_plans_effect_not_yet_executed(phase: ExecutionPhase, snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    assert RecoveryPlanner().plan(run(phase), snapshot).action == action


@pytest.mark.parametrize(("phase", "snapshot", "action"), [
    (ExecutionPhase.COMMIT_PENDING, observed(local_head_sha=OTHER, local_head_descends_from_checkpoint=True), RecoveryAction.RECORD_EXISTING_COMMIT),
    (ExecutionPhase.PUSH_PENDING, observed(remote_head_sha=HEAD), RecoveryAction.RECORD_EXISTING_PUSH),
    (ExecutionPhase.PR_PENDING, observed(remote_head_sha=HEAD, pull_requests=(PullRequestObservation(37, HEAD),)), RecoveryAction.ADOPT_PULL_REQUEST),
])
def test_reconciles_proven_effect_without_repeating(phase: ExecutionPhase, snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    assert RecoveryPlanner().plan(run(phase), snapshot).action == action


def test_blocks_ambiguous_worktree() -> None:
    assert RecoveryPlanner().plan(run(ExecutionPhase.TESTING), observed(worktree_state=WorktreeState.DIVERGENT)).action == RecoveryAction.BLOCK


def test_blocks_third_remote_sha() -> None:
    assert RecoveryPlanner().plan(run(ExecutionPhase.PUSH_PENDING), observed(remote_head_sha=THIRD)).action == RecoveryAction.BLOCK


def test_blocks_ci_for_wrong_sha() -> None:
    decision = RecoveryPlanner().plan(run(ExecutionPhase.WAITING_CI), observed(ci=CiObservation(CiState.SUCCESS, OTHER)))
    assert decision.action == RecoveryAction.BLOCK


def test_blocks_review_for_wrong_sha() -> None:
    decision = RecoveryPlanner().plan(run(ExecutionPhase.GEMINI_REVIEWING), observed(review=ReviewObservation(ReviewState.APPROVED, OTHER)))
    assert decision.action == RecoveryAction.BLOCK


def test_blocks_ambiguous_pull_requests() -> None:
    prs = (PullRequestObservation(37, HEAD), PullRequestObservation(38, HEAD))
    assert RecoveryPlanner().plan(run(ExecutionPhase.PR_PENDING), observed(remote_head_sha=HEAD, pull_requests=prs)).action == RecoveryAction.BLOCK


def test_records_merge_already_done() -> None:
    record = run(ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="APPROVED")
    snapshot = observed(merge=MergeObservation(MergeState.MERGED, HEAD, OTHER))
    assert RecoveryPlanner().plan(record, snapshot).action == RecoveryAction.RECORD_EXISTING_MERGE


def test_completes_project_already_done() -> None:
    assert RecoveryPlanner().plan(run(ExecutionPhase.PROJECT_DONE_PENDING), observed(project_done=True)).action == RecoveryAction.COMPLETE


@pytest.mark.parametrize("record, snapshot", [
    (run(ExecutionPhase.NEEDS_CHANGES, codex_session_id="session"), observed(review=ReviewObservation(ReviewState.REJECTED, HEAD))),
    (run(ExecutionPhase.NEEDS_CHANGES), observed(review=ReviewObservation(ReviewState.REJECTED, HEAD), findings_head_sha=HEAD)),
])
def test_blocks_needs_changes_without_required_recovery_data(record: RunRecord, snapshot: RecoveryObservation) -> None:
    assert RecoveryPlanner().plan(record, snapshot).action == RecoveryAction.BLOCK


def test_resumes_correction_with_session_and_findings_for_same_head() -> None:
    snapshot = observed(review=ReviewObservation(ReviewState.REJECTED, HEAD), findings_head_sha=HEAD)
    assert RecoveryPlanner().plan(run(ExecutionPhase.NEEDS_CHANGES, codex_session_id="session"), snapshot).action == RecoveryAction.RESUME_CORRECTION
