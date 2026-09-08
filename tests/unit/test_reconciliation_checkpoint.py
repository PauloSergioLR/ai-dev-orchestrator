"""Divergências que o planner bloqueia continuam visíveis após restart."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.recovery import RecoveryAction, RecoveryDecision, RecoveryObservation, WorktreeState
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.resume import ResumeError, ResumeService


class Observer:
    def observe(self, run):
        return RecoveryObservation(WorktreeState.DIVERGENT)


class Planner:
    def plan(self, run, observation):
        return RecoveryDecision(RecoveryAction.BLOCK, "PR remoto fechado ou HEAD divergente")


def test_bloqueio_do_planner_vira_checkpoint_humano_persistente(tmp_path: Path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(47, branch="work/antiga", worktree_path=str(tmp_path), base_ref="main")
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="Gates", codex_session_id="sessao")
    service = ResumeService(store, Observer(), Planner(), object())

    with pytest.raises(ResumeError, match="fechado"):
        service.resume(47)

    persisted = SqliteExecutionStore(tmp_path / "state.db").get(run.id)
    assert persisted.phase is ExecutionPhase.HUMAN_REQUIRED
    assert persisted.id == run.id and persisted.codex_session_id == "sessao"
    assert store.events(run.id)[-1].phase is ExecutionPhase.HUMAN_REQUIRED
