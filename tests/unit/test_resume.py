"""Integração controlada do loop de retomada com SQLite real."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.antigravity import AntigravityError
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, ProjectState,
    PullRequestObservation, PullRequestState, RecoveryObservation, RecoveryPolicy,
    WorktreeState,
)
from ai_dev_orchestrator.domain.review import ReviewVerdict, StructuredReview
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import CommitResult, RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.services.resume import ResumeError, ResumeService

OLD = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
URL = "https://github.com/owner/repo/pull/37"
POLICY = RecoveryPolicy("owner/repo", "main", True, 3)


def pr(head: str = HEAD) -> PullRequestObservation:
    return PullRequestObservation(37, URL, "owner/repo", "main", "feat/recovery", head,
                                  PullRequestState.OPEN)


class Effects:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def called(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def prepare_worktree(self, run: RunRecord) -> str:
        self.called("prepare")
        return HEAD

    def start_codex(self, run: RunRecord) -> str:
        self.called("start")
        return "session"

    def resume_codex(self, run: RunRecord) -> str:
        self.called("resume")
        return run.codex_session_id or ""

    def run_local_gates(self, run: RunRecord) -> None:
        self.called("gates")

    def create_commit(self, run: RunRecord) -> CommitResult:
        self.called("commit")
        return CommitResult(HEAD, run.current_head_sha or "")

    def push_branch(self, run: RunRecord) -> None:
        self.called("push")

    def create_pull_request(self, run: RunRecord) -> PullRequestObservation:
        self.called("create_pr")
        return pr(run.current_head_sha or HEAD)

    def wait_for_ci(self, run: RunRecord) -> CiObservation:
        self.called("ci")
        return CiObservation(CiState.SUCCESS, run.current_head_sha)

    def review_head(self, run: RunRecord, prior_findings: tuple[object, ...]) -> StructuredReview:
        self.called("review")
        return StructuredReview(ReviewVerdict.APPROVED, (), run.current_head_sha or "", "ok")

    def resume_correction(self, run: RunRecord, findings: tuple[object, ...]) -> str:
        self.called("correction")
        return run.codex_session_id or ""

    def merge_pull_request(self, run: RunRecord) -> MergeObservation:
        self.called("merge")
        return MergeObservation(MergeState.MERGED, run.reviewed_head_sha, MERGE)

    def mark_project_done(self, run: RunRecord) -> None:
        self.called("done")


class Observer:
    def __init__(self, function) -> None:
        self.function = function
        self.calls = 0

    def observe(self, run: RunRecord) -> RecoveryObservation:
        self.calls += 1
        return self.function(run)


def service(store: SqliteExecutionStore, observer: Observer, effects: Effects) -> ResumeService:
    return ResumeService(store, observer, RecoveryPlanner(POLICY),
                         RecoveryExecutor(POLICY, store, effects))


def create(store: SqliteExecutionStore) -> RunRecord:
    return store.create(37, project_item_id="item", branch="feat/recovery",
                        worktree_path="C:/worktree", base_ref="main")


def advance(store: SqliteExecutionStore, phase: ExecutionPhase) -> RunRecord:
    run = create(store)
    if phase == ExecutionPhase.PREPARING:
        return run
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="worktree",
                           current_head_sha=OLD)
    if phase == ExecutionPhase.CODEX_RUNNING:
        return run
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="codex",
                           codex_session_id="session")
    if phase == ExecutionPhase.TESTING:
        return run
    run = store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary="gates")
    if phase == ExecutionPhase.COMMIT_PENDING:
        return run
    run = store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary="commit",
                           current_head_sha=HEAD)
    if phase == ExecutionPhase.PUSH_PENDING:
        return run
    run = store.transition(run.id, ExecutionPhase.PR_PENDING, summary="push")
    if phase == ExecutionPhase.PR_PENDING:
        return run
    return store.transition(run.id, ExecutionPhase.WAITING_CI, summary="pr",
                            pull_request_number=37, pull_request_url=URL)


def test_resume_rejects_missing_and_terminal_runs(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    value = service(store, Observer(lambda _run: pytest.fail("não deve observar")), Effects())
    with pytest.raises(ResumeError, match="Nenhuma execução ativa"):
        value.resume(37)
    run = create(store)
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="w", current_head_sha=HEAD)
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="c", codex_session_id="session")
    run = store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary="g")
    run = store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary="c")
    run = store.transition(run.id, ExecutionPhase.PR_PENDING, summary="p")
    run = store.transition(run.id, ExecutionPhase.WAITING_CI, summary="pr")
    run = store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="ci")
    run = store.transition(run.id, ExecutionPhase.APPROVED_AWAITING_ACTION, summary="fim")

    with pytest.raises(ResumeError, match="terminal"):
        value.resume(37)


def test_preparing_existing_worktree_keeps_execution_and_does_not_prepare_again(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = create(store)
    effects = Effects()

    def snapshot(run: RunRecord) -> RecoveryObservation:
        if run.phase == ExecutionPhase.PREPARING:
            return RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD)
        if run.phase == ExecutionPhase.CODEX_RUNNING:
            return RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD)
        if run.phase == ExecutionPhase.TESTING:
            raise RuntimeError("parada controlada do teste")
        raise AssertionError(run.phase)

    with pytest.raises(ResumeError, match="parada controlada"):
        service(store, Observer(snapshot), effects).resume(37)

    persisted = store.get(original.id)
    assert persisted.id == original.id
    assert persisted.codex_session_id == "session"
    assert effects.calls == {"start": 1}


def test_testing_reexecutes_gates_before_commit(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    advance(store, ExecutionPhase.TESTING)
    effects = Effects()

    def snapshot(run: RunRecord) -> RecoveryObservation:
        if run.phase == ExecutionPhase.TESTING:
            return RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=OLD)
        raise RuntimeError("fim")

    with pytest.raises(ResumeError, match="fim"):
        service(store, Observer(snapshot), effects).resume(37)

    assert effects.calls == {"gates": 1}
    assert store.get_active_for_issue(37).phase == ExecutionPhase.COMMIT_PENDING  # type: ignore[union-attr]


def test_crash_boundaries_are_reconciled_without_repeating_remote_mutations(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = advance(store, ExecutionPhase.COMMIT_PENDING)
    effects = Effects()

    def snapshot(run: RunRecord) -> RecoveryObservation:
        common = {"worktree_state": WorktreeState.CONVERGENT,
                  "local_head_sha": HEAD, "local_head_parent_sha": OLD,
                  "remote_head_sha": HEAD}
        pulls = (pr(),) if run.phase in {
            ExecutionPhase.PR_PENDING, ExecutionPhase.WAITING_CI,
            ExecutionPhase.GEMINI_REVIEWING,
            ExecutionPhase.PROJECT_DONE_PENDING,
        } else ()
        if run.phase == ExecutionPhase.MERGE_PENDING:
            pulls = (PullRequestObservation(
                37, URL, "owner/repo", "main", "feat/recovery", HEAD,
                PullRequestState.MERGED,
            ),)
        ci = CiObservation(CiState.SUCCESS, HEAD) if run.phase in {
            ExecutionPhase.WAITING_CI, ExecutionPhase.GEMINI_REVIEWING,
            ExecutionPhase.MERGE_PENDING,
        } else CiObservation()
        merge = MergeObservation(MergeState.MERGED, HEAD, MERGE) if run.phase == ExecutionPhase.MERGE_PENDING else MergeObservation(MergeState.OPEN)
        project = ProjectState.DONE if run.phase == ExecutionPhase.PROJECT_DONE_PENDING else ProjectState.UNKNOWN
        return RecoveryObservation(**common, pull_requests=pulls, ci=ci, merge=merge,
                                   project_state=project)

    result = service(store, Observer(snapshot), effects).resume(37)

    assert result.execution_id == original.id
    assert result.phase == ExecutionPhase.COMPLETED.value
    assert result.codex_session_id == "session"
    assert result.pull_request_number == 37
    assert effects.calls == {"review": 1}
    assert store.get(original.id).merge_commit_sha == MERGE


def test_pending_ci_returns_recoverable_result_without_false_cycle(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    advance(store, ExecutionPhase.WAITING_CI)
    effects = Effects()
    effects.wait_for_ci = lambda run: (effects.called("ci") or CiObservation(CiState.PENDING, HEAD))  # type: ignore[method-assign]
    snapshot = RecoveryObservation(
        WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
        pull_requests=(pr(),), ci=CiObservation(CiState.PENDING, HEAD),
    )

    result = service(store, Observer(lambda _run: snapshot), effects).resume(37)

    assert result.phase == ExecutionPhase.WAITING_CI.value
    assert effects.calls == {"ci": 1}


def test_protocol_failure_in_review_retries_only_same_head(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, ExecutionPhase.WAITING_CI)
    original = store.transition(
        run.id,
        ExecutionPhase.GEMINI_REVIEWING,
        summary="ci",
        ci_head_sha=HEAD,
    )
    snapshot = RecoveryObservation(
        WorktreeState.CONVERGENT,
        local_head_sha=HEAD,
        remote_head_sha=HEAD,
        pull_requests=(pr(),),
        ci=CiObservation(CiState.SUCCESS, HEAD),
    )

    class ProtocolFailureEffects(Effects):
        def review_head(self, run, prior_findings):
            self.called("review")
            raise AntigravityError(
                "Falha do contrato estruturado do reviewer: SUCCESS sem structured_output"
            )

    failed_effects = ProtocolFailureEffects()
    with pytest.raises(ResumeError, match="contrato estruturado"):
        service(store, Observer(lambda _run: snapshot), failed_effects).resume(37)

    preserved = store.get(original.id)
    assert preserved.id == original.id
    assert preserved.phase is ExecutionPhase.GEMINI_REVIEWING
    assert preserved.branch == original.branch
    assert preserved.codex_session_id == original.codex_session_id
    assert preserved.pull_request_number == original.pull_request_number
    assert preserved.current_head_sha == HEAD
    assert failed_effects.calls == {"review": 1}

    recovered_effects = Effects()
    review_only_policy = RecoveryPolicy("owner/repo", "main", False, 3)
    result = ResumeService(
        store,
        Observer(lambda _run: snapshot),
        RecoveryPlanner(review_only_policy),
        RecoveryExecutor(review_only_policy, store, recovered_effects),
    ).resume(37)

    assert result.execution_id == original.id
    assert result.phase == ExecutionPhase.APPROVED_AWAITING_ACTION.value
    assert result.current_head_sha == HEAD
    assert result.pull_request_number == original.pull_request_number
    assert result.codex_session_id == original.codex_session_id
    assert recovered_effects.calls == {"review": 1}


def test_legacy_merging_reconciles_existing_merge_without_mutation(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = create(store)
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="w",
                           current_head_sha=HEAD)
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="c",
                           codex_session_id="session")
    run = store.transition(run.id, ExecutionPhase.PUBLISHING, summary="publicação")
    run = store.transition(
        run.id, ExecutionPhase.WAITING_CI, summary="pr", pull_request_number=37,
        pull_request_url=URL,
    )
    run = store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="ci",
                           ci_head_sha=HEAD)
    store.transition(
        run.id, ExecutionPhase.MERGING, summary="review", reviewed_head_sha=HEAD,
        review_verdict="APPROVED",
    )
    effects = Effects()

    def snapshot(current: RunRecord) -> RecoveryObservation:
        if current.phase == ExecutionPhase.MERGING:
            return RecoveryObservation(
                WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
                pull_requests=(PullRequestObservation(
                    37, URL, "owner/repo", "main", "feat/recovery", HEAD,
                    PullRequestState.MERGED,
                ),),
                ci=CiObservation(CiState.SUCCESS, HEAD),
                merge=MergeObservation(MergeState.MERGED, HEAD, MERGE),
            )
        return RecoveryObservation(
            WorktreeState.ABSENT, project_state=ProjectState.DONE
        )

    result = service(store, Observer(snapshot), effects).resume(37)

    assert result.phase == ExecutionPhase.COMPLETED.value
    assert effects.calls == {}
    assert store.get(run.id).merge_commit_sha == MERGE


def test_legacy_publishing_reconciles_push_before_pr(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = create(store)
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="w",
                           current_head_sha=HEAD)
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="c",
                           codex_session_id="session")
    original = store.transition(run.id, ExecutionPhase.PUBLISHING, summary="publicação")
    effects = Effects()

    def snapshot(current: RunRecord) -> RecoveryObservation:
        if current.phase == ExecutionPhase.PUBLISHING:
            return RecoveryObservation(
                WorktreeState.CONVERGENT, local_head_sha=HEAD,
                local_head_parent_sha=OLD, remote_head_sha=HEAD,
            )
        raise RuntimeError("checkpoint granular alcançado")

    with pytest.raises(ResumeError, match="checkpoint granular"):
        service(store, Observer(snapshot), effects).resume(37)

    persisted = store.get(original.id)
    assert persisted.phase == ExecutionPhase.PR_PENDING
    assert effects.calls == {}


def test_keyboard_interrupt_is_checkpointed_without_marking_failed(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, ExecutionPhase.TESTING)
    effects = Effects()

    with pytest.raises(KeyboardInterrupt):
        service(
            store,
            Observer(lambda _run: (_ for _ in ()).throw(KeyboardInterrupt())),
            effects,
        ).resume(37)

    persisted = store.get(run.id)
    assert persisted.phase == ExecutionPhase.TESTING
    assert store.events(run.id)[-1].summary == "Retomada interrompida"
    assert effects.calls == {}


def test_observation_error_runs_no_effect(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    advance(store, ExecutionPhase.TESTING)
    effects = Effects()

    with pytest.raises(ResumeError, match="observar"):
        service(store, Observer(lambda _run: (_ for _ in ()).throw(RuntimeError("offline"))), effects).resume(37)

    assert effects.calls == {}
