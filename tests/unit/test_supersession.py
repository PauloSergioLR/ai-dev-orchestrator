"""Supersessão explícita não apaga identidade nem libera órfãos silenciosamente."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    MergeObservation, MergeState, PullRequestObservation, PullRequestState,
    RecoveryObservation, WorktreeState,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.supersession import SupersessionError, SupersessionService


HEAD = "a" * 40
REMOTE = "b" * 40
URL = "https://github.com/acme/repo/pull/49"


@dataclass
class Observer:
    state: PullRequestState = PullRequestState.CLOSED
    merge: MergeState = MergeState.CLOSED
    remote: str | None = REMOTE
    calls: int = 0

    def observe(self, run: RunRecord) -> RecoveryObservation:
        self.calls += 1
        return RecoveryObservation(
            WorktreeState.DIVERGENT,
            remote_head_sha=self.remote,
            pull_requests=(PullRequestObservation(
                run.pull_request_number or 49, run.pull_request_url or URL, "acme/repo",
                "main", run.branch or "work/antigo", HEAD, self.state,
            ),),
            merge=MergeObservation(self.merge),
        )


def stored(tmp_path: Path, phase: ExecutionPhase) -> tuple[SqliteExecutionStore, RunRecord]:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(45, branch="work/antigo", worktree_path=str(tmp_path), base_ref="main")
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", current_head_sha=HEAD)
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="Gates", codex_session_id="sessao")
    run = store.checkpoint(run.id, summary="PR observado", pull_request_number=49, pull_request_url=URL)
    if phase is ExecutionPhase.FAILED:
        run = store.transition(run.id, ExecutionPhase.FAILED, summary="Rede", quota_provider="codex", quota_classification="NETWORK_ERROR")
    return store, run


@pytest.mark.parametrize("phase", [ExecutionPhase.FAILED, ExecutionPhase.TESTING])
def test_pr_fechado_sem_merge_pode_ser_supersedido_preservando_historia(tmp_path, phase):
    store, original = stored(tmp_path, phase)
    service = SupersessionService(store, Observer())

    result = service.supersede(45, "PR antigo fechado; refazer sobre main token=segredo")

    assert result.id == original.id
    assert result.phase is ExecutionPhase.SUPERSEDED
    assert result.branch == original.branch and result.worktree_path == original.worktree_path
    assert result.codex_session_id == "sessao" and result.pull_request_number == 49
    assert result.current_head_sha == HEAD
    assert store.list_active() == ()
    assert store.list_historical_candidates() == ()
    assert store.list_reconciliation_required() == ()
    events = store.events(original.id)
    assert events[-1].previous_phase is phase and events[-1].phase is ExecutionPhase.SUPERSEDED
    assert "segredo" not in events[-1].summary and "[redigido]" in events[-1].summary
    assert store.get(original.id).id == original.id


def test_novo_execution_id_so_e_permitido_depois_da_supersessao(tmp_path):
    store, original = stored(tmp_path, ExecutionPhase.FAILED)
    assert store.list_reconciliation_required() == (original,)
    service = SupersessionService(store, Observer())
    service.supersede(45, "Issue será refeita")

    replacement = store.create(45, branch="work/novo")

    assert replacement.id != original.id
    assert replacement.phase is ExecutionPhase.PREPARING
    assert store.get(original.id).phase is ExecutionPhase.SUPERSEDED


@pytest.mark.parametrize(
    ("state", "merge", "message"),
    [
        (PullRequestState.MERGED, MergeState.MERGED, "mergeado"),
        (PullRequestState.OPEN, MergeState.OPEN, "recuperação normal"),
        (PullRequestState.CLOSED, MergeState.UNKNOWN, "não permite"),
    ],
)
def test_estado_remoto_nao_elegivel_bloqueia_sem_alterar_run(tmp_path, state, merge, message):
    store, original = stored(tmp_path, ExecutionPhase.TESTING)
    service = SupersessionService(store, Observer(state, merge))

    with pytest.raises(SupersessionError, match=message):
        service.supersede(45, "motivo")

    assert store.get(original.id).phase is ExecutionPhase.TESTING
    assert len(store.events(original.id)) == 4


def test_estado_remoto_desconhecido_e_multiplo_pr_bloqueiam(tmp_path):
    store, original = stored(tmp_path, ExecutionPhase.FAILED)

    class BrokenObserver:
        def observe(self, run):
            from ai_dev_orchestrator.services.recovery_observer import RecoveryObservationError
            raise RecoveryObservationError("offline")

    with pytest.raises(SupersessionError, match="desconhecido"):
        SupersessionService(store, BrokenObserver()).preview(45)
    assert store.get(original.id).phase is ExecutionPhase.FAILED

    class AmbiguousObserver:
        def observe(self, run):
            first = PullRequestObservation(49, URL, "acme/repo", "main", "work/antigo", HEAD, PullRequestState.CLOSED)
            second = PullRequestObservation(50, "https://github.com/acme/repo/pull/50", "acme/repo", "main", "work/antigo", HEAD, PullRequestState.CLOSED)
            return RecoveryObservation(WorktreeState.CONVERGENT, pull_requests=(first, second), merge=MergeObservation(MergeState.CLOSED))

    with pytest.raises(SupersessionError, match="ambíguos"):
        SupersessionService(store, AmbiguousObserver()).preview(45)
    assert store.get(original.id).phase is ExecutionPhase.FAILED


def test_motivo_vazio_e_reinicio_nao_alteram_decisao(tmp_path):
    store, original = stored(tmp_path, ExecutionPhase.FAILED)
    service = SupersessionService(store, Observer())
    with pytest.raises(SupersessionError, match="motivo"):
        service.supersede(45, "  ")
    service.supersede(45, "substituída")

    reopened = SqliteExecutionStore(tmp_path / "state.db")
    assert reopened.get(original.id).phase is ExecutionPhase.SUPERSEDED
    assert len(reopened.events(original.id)) == 6
