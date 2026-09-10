"""Cobertura do agendamento opcional de execuções independentes."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.services.supervisor import SupervisorError, SupervisorService, _exclusive_lock
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
            raise AssertionError("não há vaga")

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
