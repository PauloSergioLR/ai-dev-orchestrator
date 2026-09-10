"""Cobertura do agendamento opcional de execuções independentes."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.supervisor import SupervisorError, SupervisorService, _exclusive_lock
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.work import WorkResult


def config(tmp_path: Path, maximum: int) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_branch": "main"},
        execution={"max_attempts": 1, "max_parallel_runs": maximum, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )


def waiting(issue: int, phase: ExecutionPhase = ExecutionPhase.WAITING_PROVIDER):
    return SimpleNamespace(
        id=f"run-{issue}", issue_number=issue, phase=phase,
        quota_retry_at=None, quota_observed_at=None,
    )


class Store:
    def __init__(self) -> None:
        self.active: list[object] = []

    def list_active(self):
        return tuple(self.active)

    def get_latest_for_issue(self, issue: int):
        return None

    def get_active_for_issue(self, issue: int):
        return next((run for run in self.active if run.issue_number == issue), None)

    def checkpoint(self, *args, **kwargs):
        raise AssertionError("checkpoint não deveria ser gravado")


def test_parallel_scheduler_fills_only_available_slots_in_stable_order(tmp_path: Path) -> None:
    store = Store()

    class Work:
        selected = [3, 9, 12]
        calls: list[frozenset[int]] = []

        def start_next(self, excluded: frozenset[int]):
            self.calls.append(excluded)
            if not self.selected:
                return None
            store.active.append(waiting(self.selected.pop(0)))
            return WorkResult(resumed=False)

        def resume_issue(self, issue: int):
            raise AssertionError("quota sem retry não deve ser retomada")

    def stop(_: float) -> None:
        raise KeyboardInterrupt

    work = Work()
    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 2), work, store, stop).watch()

    assert work.calls == [frozenset(), frozenset({3})]
    assert [run.issue_number for run in store.active] == [3, 9]


def test_parallel_scheduler_does_not_stop_other_run_for_human_required(tmp_path: Path) -> None:
    store = Store()
    human, independent = waiting(3, ExecutionPhase.HUMAN_REQUIRED), waiting(9)
    store.active.extend((human, independent))
    assessed: list[int] = []

    class Escalation:
        def assess(self, run):
            assessed.append(run.issue_number)
            return run

    class Work:
        def start_next(self, excluded):
            return None

        def resume_issue(self, issue):
            raise AssertionError("as duas execuções estão aguardando")

    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 2), Work(), store, lambda _: (_ for _ in ()).throw(KeyboardInterrupt), Escalation()).watch()

    assert assessed == [3]
    assert [run.issue_number for run in store.active] == [3, 9]


def test_watch_lock_fails_closed_for_second_supervisor(tmp_path: Path) -> None:
    lock = tmp_path / "state.watch.lock"
    with _exclusive_lock(lock):
        with pytest.raises(SupervisorError, match="instância"):
            with _exclusive_lock(lock):
                pass
    assert not lock.exists()


def test_default_sequential_mode_uses_legacy_single_work_call(tmp_path: Path) -> None:
    store = Store()

    class Work:
        calls = 0

        def work(self):
            self.calls += 1
            return None

        def start_next(self, excluded):
            raise AssertionError("modo sequencial não seleciona vagas paralelas")

    work = Work()
    SupervisorService(config(tmp_path, 1), work, store).watch()

    assert work.calls == 1


def test_parallel_runs_keep_persisted_identity_separate(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    first = store.create(3, branch="work/primeira", worktree_path=str(tmp_path / "first"), base_ref="main")
    second = store.create(9, branch="work/segunda", worktree_path=str(tmp_path / "second"), base_ref="main")
    first = store.checkpoint(first.id, summary="sessão", codex_session_id="session-3", pull_request_number=30, current_head_sha="a" * 40)
    second = store.checkpoint(second.id, summary="sessão", codex_session_id="session-9", pull_request_number=90, current_head_sha="b" * 40)

    assert (first.branch, first.worktree_path, first.codex_session_id, first.pull_request_number, first.current_head_sha) != (
        second.branch, second.worktree_path, second.codex_session_id, second.pull_request_number, second.current_head_sha
    )


def test_quota_checkpoint_preserves_the_same_run_identity(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(3, branch="work/quota", worktree_path=str(tmp_path / "quota"), base_ref="main")
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="session-3")
    waiting_run = store.transition(
        run.id, ExecutionPhase.WAITING_CODEX_QUOTA, summary="quota",
        quota_provider="codex", quota_classification="RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert waiting_run.id == run.id
    assert waiting_run.codex_session_id == "session-3"
    assert waiting_run.branch == "work/quota"


def test_parallel_waiting_resume_sleeps_instead_of_busy_loop(tmp_path: Path) -> None:
    store = Store()
    store.active.append(waiting(3, ExecutionPhase.WAITING_CI))

    class Work:
        calls = 0

        def resume_issue(self, issue):
            self.calls += 1
            return WorkResult(resumed=True, resume=SimpleNamespace(issue_number=issue, phase="WAITING_CI"))

        def start_next(self, excluded):
            return None

    work = Work()
    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 2), work, store, lambda _: (_ for _ in ()).throw(KeyboardInterrupt)).watch()

    assert work.calls == 1


def test_parallel_start_quota_error_keeps_other_runs_alive(tmp_path: Path) -> None:
    store = Store()
    store.active.append(waiting(3))

    class Work:
        def resume_issue(self, issue):
            raise AssertionError("espera sem retry não deve retomar")

        def start_next(self, excluded):
            store.active.append(waiting(9))
            raise RunPipelineError("quota checkpointada")

    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 2), Work(), store, lambda _: (_ for _ in ()).throw(KeyboardInterrupt)).watch()

    assert [run.issue_number for run in store.active] == [3, 9]


def test_provider_policy_checkpoint_only_after_retry_interval(tmp_path: Path) -> None:
    store = Store()
    run = waiting(3, ExecutionPhase.WAITING_CODEX_QUOTA)
    run.quota_observed_at = datetime.now(timezone.utc)
    store.active.append(run)
    cfg = config(tmp_path, 2)
    cfg.supervisor.retry_without_reset_seconds = 60

    supervisor = SupervisorService(cfg, object(), store)

    assert not supervisor._provider_ready(run)


def test_completion_of_one_run_opens_slot_for_next_issue(tmp_path: Path) -> None:
    store = Store()
    store.active.append(waiting(3, ExecutionPhase.WAITING_CI))

    class Work:
        started: list[frozenset[int]] = []

        def resume_issue(self, issue):
            store.active.clear()
            return WorkResult(resumed=True, resume=SimpleNamespace(issue_number=issue, phase="COMPLETED"))

        def start_next(self, excluded):
            self.started.append(excluded)
            store.active.append(waiting(9))
            return WorkResult(resumed=False)

    work = Work()
    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 1), work, store, lambda _: (_ for _ in ()).throw(KeyboardInterrupt))._watch_parallel()

    assert work.started == [frozenset()]
    assert [run.issue_number for run in store.active] == [9]


def test_keyboard_interrupt_preserves_all_active_checkpoints(tmp_path: Path) -> None:
    store = Store()
    first, second = waiting(3), waiting(9)
    store.active.extend((first, second))

    class Work:
        def resume_issue(self, issue):
            raise AssertionError("quota sem retry não deve retomar")

        def start_next(self, excluded):
            return None

    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config(tmp_path, 2), Work(), store, lambda _: (_ for _ in ()).throw(KeyboardInterrupt)).watch()

    assert store.active == [first, second]
