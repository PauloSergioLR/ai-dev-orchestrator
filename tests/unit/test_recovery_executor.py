"""Aplicação de decisões em store real com efeitos falsos contáveis."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, RecoveryAction,
    RecoveryDecision, RecoveryObservation, RecoveryPolicy, WorktreeState,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import (
    CommitResult, RecoveryExecutionError, RecoveryExecutor,
)

HEAD = "a" * 40
NEW_HEAD = "b" * 40


class Effects:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.session = "session"

    def _call(self, name: str):
        self.calls[name] = self.calls.get(name, 0) + 1

    def prepare_worktree(self, run):
        self._call("prepare")
        return HEAD

    def start_codex(self, run):
        self._call("start")
        return self.session

    def resume_codex(self, run):
        self._call("resume")
        return self.session

    def run_local_gates(self, run):
        self._call("gates")

    def create_commit(self, run):
        self._call("commit")
        return CommitResult(NEW_HEAD, HEAD)

    def push_branch(self, run):
        self._call("push")

    def create_pull_request(self, run):
        raise AssertionError("não esperado")

    def wait_for_ci(self, run):
        self._call("ci")
        return CiObservation(CiState.SUCCESS, run.current_head_sha)

    def review_head(self, run, prior_findings):
        raise AssertionError("não esperado")

    def resume_correction(self, run, findings):
        raise AssertionError("não esperado")

    def merge_pull_request(self, run):
        self._call("merge")
        return MergeObservation(MergeState.MERGED, run.reviewed_head_sha, NEW_HEAD)

    def mark_project_done(self, run):
        self._call("done")


def setup(tmp_path: Path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37, branch="feat/x", worktree_path="C:/worktree", base_ref="main")
    effects = Effects()
    executor = RecoveryExecutor(RecoveryPolicy("owner/repo", "main", True, 2), store, effects)
    observation = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD)
    return store, run, effects, executor, observation


def test_stale_record_has_zero_effects(tmp_path: Path) -> None:
    store, run, effects, executor, observation = setup(tmp_path)
    store.checkpoint(run.id, summary="mudou")
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, RecoveryDecision(RecoveryAction.PREPARE_WORKTREE, "preparar"), observation)
    assert effects.calls == {}


def test_block_has_zero_effects(tmp_path: Path) -> None:
    _, run, effects, executor, observation = setup(tmp_path)
    with pytest.raises(RecoveryExecutionError):
        executor.execute(run, RecoveryDecision(RecoveryAction.BLOCK, "bloqueado"), observation)
    assert effects.calls == {}


def test_prepare_start_gates_and_commit_preserve_execution(tmp_path: Path) -> None:
    store, run, effects, executor, observation = setup(tmp_path)
    prepared = executor.execute(run, RecoveryDecision(RecoveryAction.PREPARE_WORKTREE, "preparar"), observation)
    started = executor.execute(prepared, RecoveryDecision(RecoveryAction.START_CODEX, "iniciar"), observation)
    tested = executor.execute(started, RecoveryDecision(RecoveryAction.RUN_LOCAL_GATES, "gates"), observation)
    committed = executor.execute(tested, RecoveryDecision(RecoveryAction.CREATE_COMMIT, "commit"), observation)
    assert (committed.id, committed.phase, committed.current_head_sha) == (run.id, ExecutionPhase.PUSH_PENDING, NEW_HEAD)
    assert effects.calls == {"prepare": 1, "start": 1, "gates": 1, "commit": 1}


def test_record_existing_commit_and_push_have_no_effects(tmp_path: Path) -> None:
    store, run, effects, executor, observation = setup(tmp_path)
    code = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="x", current_head_sha=HEAD)
    testing = store.transition(code.id, ExecutionPhase.TESTING, summary="x", codex_session_id="session")
    pending = store.transition(testing.id, ExecutionPhase.COMMIT_PENDING, summary="x")
    snapshot = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=NEW_HEAD, local_head_parent_sha=HEAD)
    pushed = executor.execute(pending, RecoveryDecision(RecoveryAction.RECORD_EXISTING_COMMIT, "registrar"), snapshot)
    final = executor.execute(pushed, RecoveryDecision(RecoveryAction.RECORD_EXISTING_PUSH, "registrar"), observation)
    assert final.phase == ExecutionPhase.PR_PENDING
    assert effects.calls == {}


def test_wait_ci_success_and_divergence(tmp_path: Path) -> None:
    store, run, effects, executor, observation = setup(tmp_path)
    code = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="x", current_head_sha=HEAD)
    testing = store.transition(code.id, ExecutionPhase.TESTING, summary="x", codex_session_id="session")
    commit = store.transition(testing.id, ExecutionPhase.COMMIT_PENDING, summary="x")
    push = store.transition(commit.id, ExecutionPhase.PUSH_PENDING, summary="x")
    pr = store.transition(push.id, ExecutionPhase.PR_PENDING, summary="x")
    waiting = store.transition(pr.id, ExecutionPhase.WAITING_CI, summary="x", pull_request_number=37, pull_request_url="url")
    result = executor.execute(waiting, RecoveryDecision(RecoveryAction.WAIT_FOR_CI, "ci"), observation)
    assert result.phase == ExecutionPhase.GEMINI_REVIEWING and effects.calls["ci"] == 1
