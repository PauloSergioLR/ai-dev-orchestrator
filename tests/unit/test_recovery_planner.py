"""Matriz de decisões puras do planejador de recovery."""

from datetime import datetime

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation,
    CiState,
    MergeObservation,
    MergeState,
    ProjectState,
    PullRequestObservation,
    PullRequestState,
    RecoveryAction,
    RecoveryObservation,
    RecoveryPolicy,
    WorktreeState,
)
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner

HEAD = "a" * 40
OTHER = "b" * 40
THIRD = "c" * 40
BRANCH = "feat/recovery"
POLICY = RecoveryPolicy("owner/repository", "main", True)


def run(phase: ExecutionPhase, **changes: object) -> RunRecord:
    now = datetime(2026, 1, 1)
    values: dict[str, object] = {
        "id": "run", "issue_number": 37, "phase": phase, "created_at": now,
        "updated_at": now, "branch": BRANCH, "current_head_sha": HEAD,
    }
    values.update(changes)
    return RunRecord(**values)  # type: ignore[arg-type]


def observed(**changes: object) -> RecoveryObservation:
    values: dict[str, object] = {
        "worktree_state": WorktreeState.CONVERGENT, "local_head_sha": HEAD,
    }
    values.update(changes)
    return RecoveryObservation(**values)  # type: ignore[arg-type]


def pull_request(**changes: object) -> PullRequestObservation:
    values: dict[str, object] = {
        "number": 37, "url": "https://github.com/owner/repository/pull/37",
        "repository_full_name": POLICY.repository_full_name,
        "base": POLICY.pull_request_base, "head_branch": BRANCH,
        "head_sha": HEAD, "state": PullRequestState.OPEN,
    }
    values.update(changes)
    return PullRequestObservation(**values)  # type: ignore[arg-type]


def plan(record: RunRecord, snapshot: RecoveryObservation, *, auto_merge: bool = True):
    policy = RecoveryPolicy(POLICY.repository_full_name, POLICY.pull_request_base, auto_merge)
    return RecoveryPlanner(policy).plan(record, snapshot)


@pytest.mark.parametrize(
    ("phase", "record_changes", "snapshot", "action", "next_phase"),
    [
        (ExecutionPhase.PREPARING, {}, observed(worktree_state=WorktreeState.ABSENT), RecoveryAction.PREPARE_WORKTREE, None),
        (ExecutionPhase.PREPARING, {}, observed(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.CODEX_RUNNING),
        (ExecutionPhase.CODEX_RUNNING, {}, observed(), RecoveryAction.BLOCK, None),
        (ExecutionPhase.CODEX_RUNNING, {"codex_session_id": "session"}, observed(), RecoveryAction.RESUME_CODEX, None),
        (ExecutionPhase.TESTING, {}, observed(), RecoveryAction.RUN_LOCAL_GATES, None),
    ],
)
def test_plans_initial_phases(phase: ExecutionPhase, record_changes: dict[str, object], snapshot: RecoveryObservation, action: RecoveryAction, next_phase: ExecutionPhase | None) -> None:
    decision = plan(run(phase, **record_changes), snapshot)
    assert (decision.action, decision.next_phase) == (action, next_phase)


def test_records_direct_commit_already_created() -> None:
    decision = plan(run(ExecutionPhase.COMMIT_PENDING), observed(local_head_sha=OTHER, local_head_parent_sha=HEAD))
    assert decision.action == RecoveryAction.RECORD_EXISTING_COMMIT


def test_blocks_commit_with_non_direct_parent() -> None:
    decision = plan(run(ExecutionPhase.COMMIT_PENDING), observed(local_head_sha=OTHER, local_head_parent_sha=THIRD))
    assert decision.action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    ("snapshot", "action"),
    [
        (observed(remote_head_sha=HEAD), RecoveryAction.RECORD_EXISTING_PUSH),
        (observed(remote_head_sha=OTHER, remote_head_is_direct_parent_of_local=True), RecoveryAction.PUSH_BRANCH),
        (observed(remote_head_sha=THIRD), RecoveryAction.BLOCK),
    ],
)
def test_plans_push_only_from_proven_remote_relation(snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    assert plan(run(ExecutionPhase.PUSH_PENDING), snapshot).action == action


@pytest.mark.parametrize(
    "candidate",
    [
        pull_request(repository_full_name="other/repository"),
        pull_request(base="release"),
        pull_request(head_branch="feat/other"),
        pull_request(number=38),
        pull_request(url="https://github.com/owner/repository/pull/38"),
    ],
)
def test_blocks_pr_with_identity_different_from_policy_or_record(candidate: PullRequestObservation) -> None:
    record = run(ExecutionPhase.PR_PENDING, pull_request_number=37, pull_request_url="https://github.com/owner/repository/pull/37")
    snapshot = observed(remote_head_sha=HEAD, pull_requests=(candidate,))
    assert plan(record, snapshot).action == RecoveryAction.BLOCK


def test_adopts_single_pull_request_with_complete_convergent_identity() -> None:
    record = run(ExecutionPhase.PR_PENDING, pull_request_number=37, pull_request_url="https://github.com/owner/repository/pull/37")
    decision = plan(record, observed(remote_head_sha=HEAD, pull_requests=(pull_request(),)))
    assert (decision.action, decision.next_phase) == (RecoveryAction.ADOPT_PULL_REQUEST, ExecutionPhase.WAITING_CI)


@pytest.mark.parametrize(
    ("state", "action", "next_phase"),
    [
        (ProjectState.UNKNOWN, RecoveryAction.BLOCK, None),
        (ProjectState.NOT_DONE, RecoveryAction.MARK_PROJECT_DONE, None),
        (ProjectState.DONE, RecoveryAction.COMPLETE, ExecutionPhase.COMPLETED),
    ],
)
def test_project_state_is_explicit(state: ProjectState, action: RecoveryAction, next_phase: ExecutionPhase | None) -> None:
    decision = plan(run(ExecutionPhase.PROJECT_DONE_PENDING), observed(project_state=state))
    assert (decision.action, decision.next_phase) == (action, next_phase)


@pytest.mark.parametrize(
    ("record_changes", "snapshot", "action", "next_phase", "auto_merge"),
    [
        ({}, observed(), RecoveryAction.REVIEW_HEAD, None, True),
        ({"review_verdict": "APPROVED"}, observed(), RecoveryAction.BLOCK, None, True),
        ({"review_verdict": "APPROVED", "reviewed_head_sha": OTHER}, observed(), RecoveryAction.BLOCK, None, True),
        ({"review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, observed(), RecoveryAction.BLOCK, None, True),
        ({"review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, observed(findings_head_sha=HEAD), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.NEEDS_CHANGES, True),
        ({"review_verdict": "APPROVED", "reviewed_head_sha": HEAD}, observed(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.MERGE_PENDING, True),
        ({"review_verdict": "APPROVED", "reviewed_head_sha": HEAD}, observed(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.APPROVED_AWAITING_ACTION, False),
    ],
)
def test_plans_review_from_persisted_record(record_changes: dict[str, object], snapshot: RecoveryObservation, action: RecoveryAction, next_phase: ExecutionPhase | None, auto_merge: bool) -> None:
    decision = plan(run(ExecutionPhase.GEMINI_REVIEWING, **record_changes), snapshot, auto_merge=auto_merge)
    assert (decision.action, decision.next_phase) == (action, next_phase)


@pytest.mark.parametrize(
    "record_changes, snapshot, action",
    [
        ({"review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, observed(findings_head_sha=HEAD), RecoveryAction.BLOCK),
        ({"codex_session_id": "session", "review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, observed(), RecoveryAction.BLOCK),
        ({"codex_session_id": "session", "review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, observed(findings_head_sha=HEAD), RecoveryAction.RESUME_CORRECTION),
    ],
)
def test_needs_changes_requires_session_review_and_findings(record_changes: dict[str, object], snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    assert plan(run(ExecutionPhase.NEEDS_CHANGES, **record_changes), snapshot).action == action


@pytest.mark.parametrize(
    "snapshot, action",
    [
        (observed(local_head_sha=HEAD, ci=CiObservation(CiState.SUCCESS, OTHER), merge=MergeObservation(MergeState.OPEN), pull_requests=(pull_request(),)), RecoveryAction.BLOCK),
        (observed(local_head_sha=OTHER, ci=CiObservation(CiState.SUCCESS, HEAD), merge=MergeObservation(MergeState.OPEN), pull_requests=(pull_request(),)), RecoveryAction.BLOCK),
        (observed(local_head_sha=HEAD, ci=CiObservation(CiState.SUCCESS, HEAD), merge=MergeObservation(MergeState.OPEN), pull_requests=(pull_request(head_sha=OTHER),)), RecoveryAction.BLOCK),
        (observed(local_head_sha=HEAD, merge=MergeObservation(MergeState.UNKNOWN)), RecoveryAction.BLOCK),
        (observed(local_head_sha=HEAD, merge=MergeObservation(MergeState.MERGED, HEAD, OTHER)), RecoveryAction.RECORD_EXISTING_MERGE),
        (observed(local_head_sha=HEAD, ci=CiObservation(CiState.SUCCESS, HEAD), merge=MergeObservation(MergeState.OPEN), pull_requests=(pull_request(),)), RecoveryAction.MERGE_PULL_REQUEST),
    ],
)
def test_merge_requires_all_current_invariants(snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    record = run(ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="APPROVED")
    assert plan(record, snapshot).action == action
