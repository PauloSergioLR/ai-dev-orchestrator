"""RecoveryExecutor exercitado com SQLite real e efeitos contáveis."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, PullRequestObservation,
    PullRequestState, RecoveryAction, RecoveryDecision, RecoveryObservation,
    RecoveryPolicy, WorktreeState,
)
from ai_dev_orchestrator.domain.review import (
    FindingSeverity, ReviewFinding, ReviewVerdict, StructuredReview,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import (
    CommitResult, RecoveryExecutionError, RecoveryExecutor,
)

HEAD = "a" * 40
NEW_HEAD = "b" * 40
OTHER = "c" * 40
BRANCH = "feat/recovery"
URL = "https://github.com/owner/repository/pull/37"


class Effects:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.prepare_result = HEAD
        self.start_result = "session"
        self.resume_result = "session"
        self.commit_result = CommitResult(NEW_HEAD, HEAD)
        self.pr_result = pull_request()
        self.ci_result = CiObservation(CiState.SUCCESS, HEAD)
        self.review_result = review()
        self.correction_result = "session"
        self.merge_result = MergeObservation(MergeState.MERGED, HEAD, NEW_HEAD)
        self.correction_runs: list[RunRecord] = []
        self.correction_findings: tuple[ReviewFinding, ...] = ()
        self.review_prior_findings: tuple[ReviewFinding, ...] = ()
        self.raise_correction = False

    def _called(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def prepare_worktree(self, run: RunRecord) -> str:
        self._called("prepare")
        return self.prepare_result

    def start_codex(self, run: RunRecord) -> str:
        self._called("start")
        return self.start_result

    def resume_codex(self, run: RunRecord) -> str:
        self._called("resume")
        return self.resume_result

    def run_local_gates(self, run: RunRecord) -> None:
        self._called("gates")

    def create_commit(self, run: RunRecord) -> CommitResult:
        self._called("commit")
        return self.commit_result

    def push_branch(self, run: RunRecord) -> None:
        self._called("push")

    def create_pull_request(self, run: RunRecord) -> PullRequestObservation:
        self._called("create_pr")
        return self.pr_result

    def wait_for_ci(self, run: RunRecord) -> CiObservation:
        self._called("ci")
        return self.ci_result

    def review_head(self, run: RunRecord, prior_findings: tuple[ReviewFinding, ...]) -> StructuredReview:
        self._called("review")
        self.review_prior_findings = prior_findings
        return self.review_result

    def resume_correction(self, run: RunRecord, findings: tuple[ReviewFinding, ...]) -> str:
        self._called("correction")
        self.correction_runs.append(run)
        self.correction_findings = findings
        if self.raise_correction:
            raise RuntimeError("provider caiu")
        return self.correction_result

    def merge_pull_request(self, run: RunRecord) -> MergeObservation:
        self._called("merge")
        return self.merge_result

    def mark_project_done(self, run: RunRecord) -> None:
        self._called("done")


def pull_request(**changes: object) -> PullRequestObservation:
    values: dict[str, object] = {
        "number": 37, "url": URL, "repository_full_name": "owner/repository",
        "base": "main", "head_branch": BRANCH, "head_sha": HEAD,
        "state": PullRequestState.OPEN,
    }
    values.update(changes)
    return PullRequestObservation(**values)  # type: ignore[arg-type]


def finding() -> ReviewFinding:
    return ReviewFinding(FindingSeverity.HIGH, "Falha", "Descrição", "src/a.py", 7)


def review(**changes: object) -> StructuredReview:
    values: dict[str, object] = {
        "verdict": ReviewVerdict.REJECTED, "findings": (finding(),),
        "reviewed_head_sha": HEAD, "summary": "review",
    }
    values.update(changes)
    return StructuredReview(**values)  # type: ignore[arg-type]


def context(tmp_path: Path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    effects = Effects()
    executor = RecoveryExecutor(RecoveryPolicy("owner/repository", "main", True, 2), store, effects)
    observation = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD)
    return store, effects, executor, observation


def at(store: SqliteExecutionStore, phase: ExecutionPhase, **updates: object) -> RunRecord:
    run = store.create(37, project_item_id="project", branch=BRANCH, worktree_path="C:/worktree", base_ref="main")
    if phase == ExecutionPhase.PREPARING:
        return run
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="code", current_head_sha=HEAD)
    if phase == ExecutionPhase.CODEX_RUNNING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="test", codex_session_id="session")
    if phase == ExecutionPhase.TESTING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary="commit")
    if phase == ExecutionPhase.COMMIT_PENDING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary="push")
    if phase == ExecutionPhase.PUSH_PENDING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.PR_PENDING, summary="pr")
    if phase == ExecutionPhase.PR_PENDING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.WAITING_CI, summary="ci", pull_request_number=37, pull_request_url=URL)
    if phase == ExecutionPhase.WAITING_CI:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    run = store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="review")
    if phase == ExecutionPhase.GEMINI_REVIEWING:
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    if phase == ExecutionPhase.NEEDS_CHANGES:
        run = store.transition(run.id, ExecutionPhase.NEEDS_CHANGES, summary="changes")
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    if phase == ExecutionPhase.MERGE_PENDING:
        run = store.transition(run.id, ExecutionPhase.MERGE_PENDING, summary="merge")
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    if phase == ExecutionPhase.PROJECT_DONE_PENDING:
        run = store.transition(run.id, ExecutionPhase.MERGE_PENDING, summary="merge")
        run = store.transition(run.id, ExecutionPhase.PROJECT_DONE_PENDING, summary="done")
        return store.checkpoint(run.id, summary="dados", **updates) if updates else run
    raise AssertionError(f"Fase não suportada no helper: {phase}")


def decision(action: RecoveryAction, next_phase: ExecutionPhase | None = None) -> RecoveryDecision:
    return RecoveryDecision(action, "teste", next_phase)


def assert_error(call, effects: Effects) -> None:
    with pytest.raises(RecoveryExecutionError):
        call()
    assert effects.calls == {}


def test_stale_and_block_have_zero_effects(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.PREPARING)
    store.checkpoint(run.id, summary="stale")
    assert_error(lambda: executor.execute(run, decision(RecoveryAction.PREPARE_WORKTREE), observation), effects)
    current = store.get(run.id)
    assert_error(lambda: executor.execute(current, decision(RecoveryAction.BLOCK), observation), effects)


@pytest.mark.parametrize(
    "phase, action",
    [
        (ExecutionPhase.TESTING, RecoveryAction.PREPARE_WORKTREE),
        (ExecutionPhase.PREPARING, RecoveryAction.START_CODEX),
        (ExecutionPhase.PREPARING, RecoveryAction.RESUME_CODEX),
        (ExecutionPhase.PREPARING, RecoveryAction.RUN_LOCAL_GATES),
        (ExecutionPhase.TESTING, RecoveryAction.CREATE_COMMIT),
        (ExecutionPhase.TESTING, RecoveryAction.RECORD_EXISTING_COMMIT),
        (ExecutionPhase.COMMIT_PENDING, RecoveryAction.PUSH_BRANCH),
        (ExecutionPhase.COMMIT_PENDING, RecoveryAction.RECORD_EXISTING_PUSH),
        (ExecutionPhase.PUSH_PENDING, RecoveryAction.CREATE_PULL_REQUEST),
        (ExecutionPhase.PUSH_PENDING, RecoveryAction.ADOPT_PULL_REQUEST),
        (ExecutionPhase.PR_PENDING, RecoveryAction.WAIT_FOR_CI),
        (ExecutionPhase.PR_PENDING, RecoveryAction.RECORD_CI_SUCCESS),
        (ExecutionPhase.WAITING_CI, RecoveryAction.REVIEW_HEAD),
        (ExecutionPhase.GEMINI_REVIEWING, RecoveryAction.RESUME_CORRECTION),
        (ExecutionPhase.NEEDS_CHANGES, RecoveryAction.MERGE_PULL_REQUEST),
        (ExecutionPhase.NEEDS_CHANGES, RecoveryAction.RECORD_EXISTING_MERGE),
        (ExecutionPhase.MERGE_PENDING, RecoveryAction.MARK_PROJECT_DONE),
        (ExecutionPhase.MERGE_PENDING, RecoveryAction.COMPLETE),
    ],
)
def test_action_in_wrong_phase_has_zero_effects(tmp_path: Path, phase: ExecutionPhase, action: RecoveryAction) -> None:
    store, effects, executor, observation = context(tmp_path)
    assert_error(lambda: executor.execute(at(store, phase), decision(action), observation), effects)


def test_invalid_advance_has_zero_effects(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    assert_error(lambda: executor.execute(at(store, ExecutionPhase.PREPARING), decision(RecoveryAction.ADVANCE_PHASE, ExecutionPhase.COMPLETED), observation), effects)


def test_prepare_and_advance_worktree(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.PREPARING)
    prepared = executor.execute(run, decision(RecoveryAction.PREPARE_WORKTREE), observation)
    assert (prepared.id, prepared.phase, prepared.current_head_sha, effects.calls) == (run.id, ExecutionPhase.CODEX_RUNNING, HEAD, {"prepare": 1})
    store2, effects2, executor2, observation2 = context(tmp_path / "existing")
    advanced = executor2.execute(at(store2, ExecutionPhase.PREPARING), decision(RecoveryAction.ADVANCE_PHASE, ExecutionPhase.CODEX_RUNNING), observation2)
    assert (advanced.phase, advanced.current_head_sha, effects2.calls) == (ExecutionPhase.CODEX_RUNNING, HEAD, {})


def test_start_resume_and_gates(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    started = executor.execute(at(store, ExecutionPhase.CODEX_RUNNING), decision(RecoveryAction.START_CODEX), observation)
    assert (started.phase, started.codex_session_id, effects.calls) == (ExecutionPhase.TESTING, "session", {"start": 1})
    store_resume, effects_resume, executor_resume, observation_resume = context(tmp_path / "resume")
    resumed = executor_resume.execute(at(store_resume, ExecutionPhase.CODEX_RUNNING, codex_session_id="session"), decision(RecoveryAction.RESUME_CODEX), observation_resume)
    assert (resumed.phase, resumed.codex_session_id, effects_resume.calls["resume"]) == (ExecutionPhase.TESTING, "session", 1)
    store_gates, effects_gates, executor_gates, observation_gates = context(tmp_path / "gates")
    gated = executor_gates.execute(at(store_gates, ExecutionPhase.TESTING), decision(RecoveryAction.RUN_LOCAL_GATES), observation_gates)
    assert (gated.phase, effects_gates.calls["gates"]) == (ExecutionPhase.COMMIT_PENDING, 1)


def test_resume_divergent_session_does_not_transition(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.resume_result = "other"
    run = at(store, ExecutionPhase.CODEX_RUNNING, codex_session_id="session")
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.RESUME_CODEX), observation)
    assert store.get(run.id).phase == ExecutionPhase.CODEX_RUNNING and effects.calls == {"resume": 1}


def test_commit_create_record_and_cleanup(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.COMMIT_PENDING, ci_head_sha=HEAD, reviewed_head_sha=HEAD, review_verdict="APPROVED", merge_commit_sha=OTHER, merged_head_sha=HEAD)
    created = executor.execute(run, decision(RecoveryAction.CREATE_COMMIT), observation)
    assert (created.phase, created.current_head_sha, created.ci_head_sha, created.review_verdict, created.merge_commit_sha, effects.calls) == (ExecutionPhase.PUSH_PENDING, NEW_HEAD, None, None, None, {"commit": 1})
    store_record, effects_record, executor_record, _ = context(tmp_path / "record")
    recorded_run = at(store_record, ExecutionPhase.COMMIT_PENDING)
    recorded = executor_record.execute(recorded_run, decision(RecoveryAction.RECORD_EXISTING_COMMIT), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=NEW_HEAD, local_head_parent_sha=HEAD))
    assert (recorded.current_head_sha, effects_record.calls) == (NEW_HEAD, {})


def test_commit_wrong_parent_fails(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.commit_result = CommitResult(NEW_HEAD, OTHER)
    run = at(store, ExecutionPhase.COMMIT_PENDING)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.CREATE_COMMIT), observation)
    assert store.get(run.id).phase == ExecutionPhase.COMMIT_PENDING and effects.calls == {"commit": 1}


def test_push_and_recorded_push(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    pushed = executor.execute(at(store, ExecutionPhase.PUSH_PENDING), decision(RecoveryAction.PUSH_BRANCH), observation)
    assert (pushed.phase, effects.calls) == (ExecutionPhase.PR_PENDING, {"push": 1})
    store_error, effects_error, executor_error, _ = context(tmp_path / "error")
    run = at(store_error, ExecutionPhase.PUSH_PENDING)
    with pytest.raises(RecoveryExecutionError):
        executor_error.execute(run, decision(RecoveryAction.PUSH_BRANCH), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD))
    assert effects_error.calls == {}
    store_record, effects_record, executor_record, observation_record = context(tmp_path / "record")
    recorded = executor_record.execute(at(store_record, ExecutionPhase.PUSH_PENDING), decision(RecoveryAction.RECORD_EXISTING_PUSH), observation_record)
    assert recorded.phase == ExecutionPhase.PR_PENDING and effects_record.calls == {}


def test_create_and_adopt_pull_request(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    created = executor.execute(at(store, ExecutionPhase.PR_PENDING), decision(RecoveryAction.CREATE_PULL_REQUEST), observation)
    assert (created.pull_request_number, created.pull_request_url, effects.calls) == (37, URL, {"create_pr": 1})
    store_adopt, effects_adopt, executor_adopt, _ = context(tmp_path / "adopt")
    adopted = executor_adopt.execute(at(store_adopt, ExecutionPhase.PR_PENDING), decision(RecoveryAction.ADOPT_PULL_REQUEST), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, pull_requests=(pull_request(),)))
    assert (adopted.pull_request_number, adopted.pull_request_url, effects_adopt.calls) == (37, URL, {})


@pytest.mark.parametrize(
    "pr, existing",
    [
        (pull_request(repository_full_name="wrong"), False),
        (pull_request(base="release"), False),
        (pull_request(head_branch="feat/other"), False),
        (pull_request(head_sha=OTHER), False),
        (pull_request(), True),
    ],
)
def test_create_pull_request_rejects_bad_result_or_existing_identity(tmp_path: Path, pr: PullRequestObservation, existing: bool) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.pr_result = pr
    run = at(store, ExecutionPhase.PR_PENDING, pull_request_number=37 if existing else None, pull_request_url=URL if existing else None)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.CREATE_PULL_REQUEST), observation)
    assert effects.calls.get("create_pr", 0) == (0 if existing else 1)


@pytest.mark.parametrize("prs", [(), (pull_request(), pull_request(number=38, url="u"))])
def test_adopt_requires_exactly_one_pr(tmp_path: Path, prs: tuple[PullRequestObservation, ...]) -> None:
    store, effects, executor, _ = context(tmp_path)
    run = at(store, ExecutionPhase.PR_PENDING)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.ADOPT_PULL_REQUEST), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, pull_requests=prs))
    assert effects.calls == {}


@pytest.mark.parametrize("state", [CiState.ABSENT, CiState.PENDING])
def test_wait_ci_pending_stays_waiting(tmp_path: Path, state: CiState) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.ci_result = CiObservation(state)
    result = executor.execute(at(store, ExecutionPhase.WAITING_CI), decision(RecoveryAction.WAIT_FOR_CI), observation)
    assert result.phase == ExecutionPhase.WAITING_CI and effects.calls == {"ci": 1}


@pytest.mark.parametrize("ci", [CiObservation(CiState.FAILURE, HEAD), CiObservation(CiState.SUCCESS, OTHER)])
def test_wait_ci_rejects_failure_or_wrong_head(tmp_path: Path, ci: CiObservation) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.ci_result = ci
    with pytest.raises(RecoveryExecutionError):
        executor.execute(at(store, ExecutionPhase.WAITING_CI), decision(RecoveryAction.WAIT_FOR_CI), observation)
    assert effects.calls == {"ci": 1}


def test_wait_and_record_ci_success(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    waited = executor.execute(at(store, ExecutionPhase.WAITING_CI), decision(RecoveryAction.WAIT_FOR_CI), observation)
    assert (waited.phase, waited.ci_head_sha, effects.calls) == (ExecutionPhase.GEMINI_REVIEWING, HEAD, {"ci": 1})
    store_record, effects_record, executor_record, _ = context(tmp_path / "record")
    recorded = executor_record.execute(at(store_record, ExecutionPhase.WAITING_CI), decision(RecoveryAction.RECORD_CI_SUCCESS), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, ci=CiObservation(CiState.SUCCESS, HEAD)))
    assert (recorded.phase, effects_record.calls) == (ExecutionPhase.GEMINI_REVIEWING, {})


def test_review_persists_findings_and_prior_findings(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    prior_run = at(store, ExecutionPhase.GEMINI_REVIEWING)
    store.record_review(prior_run.id, review(), "prior")
    result = executor.execute(store.get(prior_run.id), decision(RecoveryAction.REVIEW_HEAD), observation)
    assert result.reviewed_head_sha == HEAD and effects.calls == {"review": 1}
    assert effects.review_prior_findings == (finding(),) and store.review_findings(result.id) == (finding(),)


@pytest.mark.parametrize("result", [review(reviewed_head_sha=OTHER), review(findings=())])
def test_review_rejects_wrong_head_or_empty_rejection(tmp_path: Path, result: StructuredReview) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.review_result = result
    run = at(store, ExecutionPhase.GEMINI_REVIEWING)
    with pytest.raises(Exception):
        executor.execute(run, decision(RecoveryAction.REVIEW_HEAD), observation)
    assert store.get(run.id).review_verdict is None and effects.calls == {"review": 1}


def needs_changes(store: SqliteExecutionStore) -> RunRecord:
    reviewing = at(store, ExecutionPhase.GEMINI_REVIEWING)
    persisted = store.record_review(reviewing.id, review(), "review")
    return store.transition(persisted.id, ExecutionPhase.NEEDS_CHANGES, summary="changes")


def test_correction_audits_before_provider_and_uses_findings(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    result = executor.execute(needs_changes(store), decision(RecoveryAction.RESUME_CORRECTION), observation)
    assert result.phase == ExecutionPhase.TESTING and effects.calls == {"correction": 1}
    assert effects.correction_runs[0].correction_attempts == 1
    assert effects.correction_runs[0].codex_session_id == "session"
    assert effects.correction_findings == (finding(),)


@pytest.mark.parametrize("raises, session", [(True, "session"), (False, "other")])
def test_correction_failure_or_session_mismatch_preserves_audit(tmp_path: Path, raises: bool, session: str) -> None:
    store, effects, executor, observation = context(tmp_path)
    effects.raise_correction, effects.correction_result = raises, session
    run = needs_changes(store)
    with pytest.raises(Exception):
        executor.execute(run, decision(RecoveryAction.RESUME_CORRECTION), observation)
    persisted = store.get(run.id)
    assert (persisted.phase, persisted.correction_attempts, effects.calls) == (ExecutionPhase.NEEDS_CHANGES, 1, {"correction": 1})


def test_correction_limit_skips_provider(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = store.checkpoint(needs_changes(store).id, summary="limit", correction_attempts=2)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.RESUME_CORRECTION), observation)
    assert effects.calls == {}


@pytest.mark.parametrize(
    "merge, action, expected_calls",
    [
        (MergeObservation(MergeState.MERGED, HEAD, NEW_HEAD), RecoveryAction.MERGE_PULL_REQUEST, {"merge": 1}),
        (MergeObservation(MergeState.MERGED, HEAD, NEW_HEAD), RecoveryAction.RECORD_EXISTING_MERGE, {}),
    ],
)
def test_merge_and_recorded_merge(tmp_path: Path, merge: MergeObservation, action: RecoveryAction, expected_calls: dict[str, int]) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="APPROVED")
    effects.merge_result = merge
    result = executor.execute(run, decision(action), RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, merge=merge))
    assert (result.phase, result.merged_head_sha, result.merge_commit_sha, effects.calls) == (ExecutionPhase.PROJECT_DONE_PENDING, HEAD, NEW_HEAD, expected_calls)


@pytest.mark.parametrize("merge", [MergeObservation(MergeState.MERGED, OTHER, NEW_HEAD), MergeObservation(MergeState.MERGED, HEAD, None)])
def test_merge_rejects_bad_result(tmp_path: Path, merge: MergeObservation) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="APPROVED")
    effects.merge_result = merge
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.MERGE_PULL_REQUEST), observation)
    assert effects.calls == {"merge": 1}


def test_merge_requires_approved_review_before_effect(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.MERGE_PENDING, reviewed_head_sha=HEAD, review_verdict="REJECTED")
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.MERGE_PULL_REQUEST), observation)
    assert effects.calls == {}


def test_project_done_and_complete(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.PROJECT_DONE_PENDING, reviewed_head_sha=HEAD, merged_head_sha=HEAD, merge_commit_sha=NEW_HEAD)
    done = executor.execute(run, decision(RecoveryAction.MARK_PROJECT_DONE), observation)
    assert (done.phase, done.project_status, effects.calls) == (ExecutionPhase.COMPLETED, "Done", {"done": 1})
    store2, effects2, executor2, observation2 = context(tmp_path / "complete")
    completed = executor2.execute(at(store2, ExecutionPhase.PROJECT_DONE_PENDING, reviewed_head_sha=HEAD, merged_head_sha=HEAD, merge_commit_sha=NEW_HEAD), decision(RecoveryAction.COMPLETE), observation2)
    assert completed.phase == ExecutionPhase.COMPLETED and effects2.calls == {}


def test_project_done_requires_proven_merge(tmp_path: Path) -> None:
    store, effects, executor, observation = context(tmp_path)
    run = at(store, ExecutionPhase.PROJECT_DONE_PENDING, reviewed_head_sha=HEAD)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, decision(RecoveryAction.MARK_PROJECT_DONE), observation)
    assert effects.calls == {}
