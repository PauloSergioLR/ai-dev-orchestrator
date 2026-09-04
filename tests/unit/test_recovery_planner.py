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
PR_URL = "https://github.com/owner/repository/pull/37"
POLICY = RecoveryPolicy("owner/repository", "main", True, 3)


def run(phase: ExecutionPhase, **changes: object) -> RunRecord:
    now = datetime(2026, 1, 1)
    values: dict[str, object] = {
        "id": "run", "issue_number": 37, "phase": phase, "created_at": now,
        "updated_at": now, "branch": BRANCH, "worktree_path": "C:/worktree",
        "base_ref": "main", "current_head_sha": HEAD,
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
        "number": 37, "url": PR_URL, "repository_full_name": POLICY.repository_full_name,
        "base": POLICY.pull_request_base, "head_branch": BRANCH, "head_sha": HEAD,
        "state": PullRequestState.OPEN,
    }
    values.update(changes)
    return PullRequestObservation(**values)  # type: ignore[arg-type]


def published_run(phase: ExecutionPhase, **changes: object) -> RunRecord:
    return run(phase, pull_request_number=37, pull_request_url=PR_URL, **changes)


def with_pr(**changes: object) -> RecoveryObservation:
    return observed(pull_requests=(pull_request(),), **changes)


def plan(record: RunRecord, snapshot: RecoveryObservation, *, auto_merge: bool = True):
    policy = RecoveryPolicy(POLICY.repository_full_name, POLICY.pull_request_base, auto_merge, 3)
    return RecoveryPlanner(policy).plan(record, snapshot)


@pytest.mark.parametrize("field", ["branch", "worktree_path", "base_ref"])
def test_preparing_requires_complete_worktree_identity(field: str) -> None:
    decision = plan(run(ExecutionPhase.PREPARING, **{field: None}), observed(worktree_state=WorktreeState.ABSENT))
    assert decision.action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "snapshot, action, next_phase",
    [
        (observed(worktree_state=WorktreeState.ABSENT), RecoveryAction.PREPARE_WORKTREE, None),
        (observed(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.CODEX_RUNNING),
    ],
)
def test_preparing_uses_observed_worktree(snapshot: RecoveryObservation, action: RecoveryAction, next_phase: ExecutionPhase | None) -> None:
    decision = plan(run(ExecutionPhase.PREPARING), snapshot)
    assert (decision.action, decision.next_phase) == (action, next_phase)


@pytest.mark.parametrize(
    "session, action",
    [(None, RecoveryAction.START_CODEX), ("session", RecoveryAction.RESUME_CODEX)],
)
def test_codex_phase_starts_only_when_session_is_absent(session: str | None, action: RecoveryAction) -> None:
    assert plan(run(ExecutionPhase.CODEX_RUNNING, codex_session_id=session), observed()).action == action


def test_commit_accepts_dirty_worktree_including_new_files() -> None:
    decision = plan(run(ExecutionPhase.COMMIT_PENDING), observed(has_worktree_changes=True))
    assert decision.action == RecoveryAction.CREATE_COMMIT


def test_records_only_direct_commit_already_created() -> None:
    decision = plan(run(ExecutionPhase.COMMIT_PENDING), observed(local_head_sha=OTHER, local_head_parent_sha=HEAD))
    assert decision.action == RecoveryAction.RECORD_EXISTING_COMMIT


@pytest.mark.parametrize(
    "snapshot, action",
    [
        (observed(remote_head_sha=None), RecoveryAction.PUSH_BRANCH),
        (observed(remote_head_sha=OTHER, local_head_parent_sha=OTHER), RecoveryAction.PUSH_BRANCH),
        (observed(remote_head_sha=THIRD, local_head_parent_sha=OTHER), RecoveryAction.BLOCK),
        (observed(remote_head_sha=HEAD), RecoveryAction.RECORD_EXISTING_PUSH),
    ],
)
def test_push_uses_explicit_sha_relations(snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    assert plan(run(ExecutionPhase.PUSH_PENDING), snapshot).action == action


def test_blocks_partially_persisted_pr_identity() -> None:
    decision = plan(run(ExecutionPhase.PR_PENDING, pull_request_number=37), observed(remote_head_sha=HEAD))
    assert decision.action == RecoveryAction.BLOCK


@pytest.mark.parametrize("snapshot", [observed(), observed(pull_requests=(pull_request(head_sha=OTHER),))])
def test_waiting_ci_requires_persisted_and_convergent_pr(snapshot: RecoveryObservation) -> None:
    record = published_run(ExecutionPhase.WAITING_CI) if snapshot.pull_requests else run(ExecutionPhase.WAITING_CI)
    assert plan(record, snapshot).action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "ci, action",
    [
        (CiObservation(CiState.PENDING, HEAD), RecoveryAction.WAIT_FOR_CI),
        (CiObservation(CiState.SUCCESS, HEAD), RecoveryAction.RECORD_CI_SUCCESS),
    ],
)
def test_waiting_ci_with_convergent_pr_plans_from_ci(ci: CiObservation, action: RecoveryAction) -> None:
    assert plan(published_run(ExecutionPhase.WAITING_CI), with_pr(ci=ci)).action == action


@pytest.mark.parametrize(
    "phase, record_changes",
    [
        (ExecutionPhase.GEMINI_REVIEWING, {}),
        (ExecutionPhase.NEEDS_CHANGES, {"codex_session_id": "session", "review_verdict": "REJECTED", "reviewed_head_sha": HEAD}),
    ],
)
def test_review_phases_require_convergent_pr(phase: ExecutionPhase, record_changes: dict[str, object]) -> None:
    assert plan(run(phase, **record_changes), observed(findings_head_sha=HEAD)).action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "phase, record_changes, snapshot",
    [
        (ExecutionPhase.PUSH_PENDING, {}, observed(local_head_sha=OTHER)),
        (ExecutionPhase.PR_PENDING, {}, observed(local_head_sha=OTHER, remote_head_sha=HEAD)),
        (ExecutionPhase.WAITING_CI, {}, with_pr(local_head_sha=OTHER, ci=CiObservation(CiState.PENDING, HEAD))),
        (ExecutionPhase.GEMINI_REVIEWING, {}, with_pr(local_head_sha=OTHER)),
        (ExecutionPhase.NEEDS_CHANGES, {"codex_session_id": "session", "review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, with_pr(local_head_sha=OTHER, findings_head_sha=HEAD)),
    ],
)
def test_published_phases_block_divergent_local_head(phase: ExecutionPhase, record_changes: dict[str, object], snapshot: RecoveryObservation) -> None:
    record = published_run(phase, **record_changes) if phase in {ExecutionPhase.WAITING_CI, ExecutionPhase.GEMINI_REVIEWING, ExecutionPhase.NEEDS_CHANGES} else run(phase, **record_changes)
    assert plan(record, snapshot).action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "record_changes",
    [
        {},
        {"project_item_id": "item", "reviewed_head_sha": HEAD, "merged_head_sha": HEAD},
        {"project_item_id": "item", "reviewed_head_sha": HEAD, "merged_head_sha": OTHER, "merge_commit_sha": THIRD},
    ],
)
def test_project_done_requires_proven_persisted_merge(record_changes: dict[str, object]) -> None:
    assert plan(run(ExecutionPhase.PROJECT_DONE_PENDING, **record_changes), observed(project_state=ProjectState.NOT_DONE)).action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "project_state, action, next_phase",
    [
        (ProjectState.NOT_DONE, RecoveryAction.MARK_PROJECT_DONE, None),
        (ProjectState.DONE, RecoveryAction.COMPLETE, ExecutionPhase.COMPLETED),
    ],
)
def test_project_done_runs_only_after_proven_merge(project_state: ProjectState, action: RecoveryAction, next_phase: ExecutionPhase | None) -> None:
    record = run(ExecutionPhase.PROJECT_DONE_PENDING, project_item_id="item", reviewed_head_sha=HEAD, merged_head_sha=HEAD, merge_commit_sha=OTHER)
    decision = plan(record, observed(project_state=project_state))
    assert (decision.action, decision.next_phase) == (action, next_phase)


@pytest.mark.parametrize(
    "record_changes, snapshot, action, next_phase, auto_merge",
    [
        ({}, with_pr(), RecoveryAction.REVIEW_HEAD, None, True),
        ({"review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, with_pr(), RecoveryAction.BLOCK, None, True),
        ({"review_verdict": "REJECTED", "reviewed_head_sha": HEAD}, with_pr(findings_head_sha=HEAD), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.NEEDS_CHANGES, True),
        ({"review_verdict": "APPROVED", "reviewed_head_sha": HEAD}, with_pr(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.MERGE_PENDING, True),
        ({"review_verdict": "APPROVED", "reviewed_head_sha": HEAD}, with_pr(), RecoveryAction.ADVANCE_PHASE, ExecutionPhase.APPROVED_AWAITING_ACTION, False),
    ],
)
def test_review_uses_persisted_result(record_changes: dict[str, object], snapshot: RecoveryObservation, action: RecoveryAction, next_phase: ExecutionPhase | None, auto_merge: bool) -> None:
    decision = plan(published_run(ExecutionPhase.GEMINI_REVIEWING, **record_changes), snapshot, auto_merge=auto_merge)
    assert (decision.action, decision.next_phase) == (action, next_phase)


def test_needs_changes_resumes_only_with_all_persisted_evidence() -> None:
    record = published_run(ExecutionPhase.NEEDS_CHANGES, codex_session_id="session", review_verdict="REJECTED", reviewed_head_sha=HEAD)
    assert plan(record, with_pr(findings_head_sha=HEAD)).action == RecoveryAction.RESUME_CORRECTION


def test_needs_changes_blocks_at_correction_limit() -> None:
    record = published_run(ExecutionPhase.NEEDS_CHANGES, codex_session_id="session", review_verdict="REJECTED", reviewed_head_sha=HEAD, correction_attempts=3)
    assert plan(record, with_pr(findings_head_sha=HEAD)).action == RecoveryAction.BLOCK


@pytest.mark.parametrize(
    "snapshot, action",
    [
        (with_pr(ci=CiObservation(CiState.SUCCESS, OTHER), merge=MergeObservation(MergeState.OPEN)), RecoveryAction.BLOCK),
        (with_pr(local_head_sha=OTHER, ci=CiObservation(CiState.SUCCESS, HEAD), merge=MergeObservation(MergeState.OPEN)), RecoveryAction.BLOCK),
        (with_pr(merge=MergeObservation(MergeState.UNKNOWN)), RecoveryAction.BLOCK),
        (with_pr(merge=MergeObservation(MergeState.MERGED, HEAD, OTHER)), RecoveryAction.RECORD_EXISTING_MERGE),
        (with_pr(ci=CiObservation(CiState.SUCCESS, HEAD), merge=MergeObservation(MergeState.OPEN)), RecoveryAction.MERGE_PULL_REQUEST),
    ],
)
def test_merge_requires_current_pr_ci_head_and_merge_state(snapshot: RecoveryObservation, action: RecoveryAction) -> None:
    record = published_run(ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="APPROVED")
    assert plan(record, snapshot).action == action
