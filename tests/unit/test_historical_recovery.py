"""Provas positivas/negativas de recovery histórico; bancos e sessões só temporários."""

from dataclasses import replace
import json

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase as Phase
from ai_dev_orchestrator.domain.recovery import (
    RecoveryObservation, WorktreeState, MergeObservation, MergeState, ProjectState,
    PullRequestState,
)
from ai_dev_orchestrator.infrastructure.codex_session import session_matches_worktree
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore, ExecutionStoreError
from ai_dev_orchestrator.services.historical_recovery import recover_historical
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.services.resume import ResumeError
from test_resume import HEAD, POLICY, pr, Observer, Effects, service
from test_provider_recovery import NOW, rejected


def historical(store):
    run = rejected(store)
    store.transition(run.id, Phase.CODEX_RUNNING, summary="correção iniciada", correction_attempts=1,
                     project_status="AI Review")
    return store.transition(run.id, Phase.FAILED, summary="Falha terminal do provider",
                            quota_provider="codex", quota_classification="NETWORK_ERROR",
                            quota_observed_at=NOW.isoformat(), last_error="codex: NETWORK_ERROR: Falha reportada pela CLI")


def evidence(run):
    return RecoveryObservation(
        WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
        pull_requests=(replace(pr(), issue_numbers=(run.issue_number,)),), findings_head_sha=HEAD,
        merge=MergeObservation(MergeState.OPEN), project_state=ProjectState.NOT_DONE,
        issue_number=run.issue_number, issue_state="OPEN", codex_session_id=run.codex_session_id,
        project_status=run.project_status,
    )


def test_failed_network_reativa_mesmo_record_com_provas(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = historical(store)
    before_findings = store.review_findings(run.id)
    before_events = store.events(run.id)
    recovered = recover_historical(store, run, Observer(lambda _: evidence(run)), RecoveryPlanner(POLICY))
    assert recovered.phase == Phase.CODEX_RUNNING
    assert replace(recovered, phase=run.phase, updated_at=run.updated_at) == run
    assert store.review_findings(run.id) == before_findings
    assert store.events(run.id)[:-1] == before_events
    assert store.events(run.id)[-1].previous_phase == Phase.FAILED
    assert store.list_active() == (recovered,) and len(store.list_history()) == 1
    from ai_dev_orchestrator.services.history import HistoryService
    assert HistoryService(store).metrics(recovered).duration == recovered.updated_at - recovered.created_at


@pytest.mark.parametrize("change", [
    {"worktree_state": WorktreeState.ABSENT}, {"worktree_state": WorktreeState.DIVERGENT},
    {"local_head_sha": "other"}, {"remote_head_sha": "other"}, {"remote_head_sha": None},
    {"pull_requests": ()}, {"pull_requests": (pr(), pr())},
    {"pull_requests": (replace(pr(), number=99),)}, {"pull_requests": (replace(pr(), head_branch="other"),)},
    {"pull_requests": (replace(pr(), head_sha="other"),)}, {"pull_requests": (replace(pr(), base="other"),)},
    {"pull_requests": (replace(pr(), state=PullRequestState.MERGED),)},
    {"merge": MergeObservation(MergeState.MERGED)}, {"merge": MergeObservation(MergeState.CLOSED)},
    {"merge": MergeObservation(MergeState.UNKNOWN)},
    {"issue_number": 99}, {"issue_state": "CLOSED"}, {"issue_state": None},
    {"codex_session_id": None}, {"codex_session_id": "other"},
    {"project_state": ProjectState.UNKNOWN}, {"project_state": ProjectState.DONE},
    {"project_status": "Ready"}, {"findings_head_sha": "other"},
    {"pull_requests": (replace(pr(), issue_numbers=(99,)),)},
])
def test_divergencias_impedem_reativacao_sem_efeitos(tmp_path, change):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = historical(store)
    events = store.events(run.id)
    observer = Observer(lambda _: replace(evidence(run), **change))
    with pytest.raises(ValueError):
        recover_historical(store, run, observer, RecoveryPlanner(POLICY))
    assert store.get(run.id) == run and store.events(run.id) == events
    assert not store.list_active()


@pytest.mark.parametrize("change", [
    {"codex_session_id": None}, {"project_item_id": None}, {"worktree_path": None},
    {"current_head_sha": None}, {"base_ref": None}, {"pull_request_url": None},
    {"quota_classification": "UNKNOWN"}, {"quota_provider": "gemini"},
    {"quota_observed_at": None}, {"merge_commit_sha": "merged"},
    {"ci_head_sha": "other"}, {"reviewed_head_sha": "other"},
])
def test_registro_sem_prova_suficiente_nao_pode_reabrir(tmp_path, change):
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = historical(store)
    # Mutação somente da representação imutável passada ao domínio.
    run = replace(original, **change)
    with pytest.raises((ValueError, ExecutionStoreError)):
        recover_historical(store, run, Observer(lambda _: evidence(run)), RecoveryPlanner(POLICY))
    assert store.get(original.id) == original


def test_store_revalida_snapshot_e_outra_execucao_ativa(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = historical(store)
    store.checkpoint(run.id, summary="intervenção concorrente", last_error="alterado")
    with pytest.raises(ExecutionStoreError, match="mudou"):
        store.reactivate_historical(run, evidence(run), RecoveryPlanner(POLICY))
    current = store.get(run.id)
    store.create(99)
    with pytest.raises(ExecutionStoreError, match="ativa"):
        store.reactivate_historical(current, evidence(current), RecoveryPlanner(POLICY))
    assert store.get(current.id).phase == Phase.FAILED


def test_transicao_generica_nao_reabre_failed(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = historical(store)
    with pytest.raises(ExecutionStoreError, match="inválida"):
        store.transition(run.id, Phase.CODEX_RUNNING, summary="atalho")
    with pytest.raises(ExecutionStoreError, match="novo run recusado"):
        store.create(run.issue_number)


def test_resume_exige_flag_e_preserva_id(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = historical(store)
    class RecoveryEffects(Effects):
        def resume_correction(self, current, findings):
            self.called("correction")
            assert current.id == run.id and current.codex_session_id == run.codex_session_id
            assert current.correction_attempts == 1 and findings
            return current.codex_session_id
    def observe(current):
        if current.phase == Phase.TESTING:
            raise RuntimeError("checkpoint alcançado")
        return evidence(current)
    effects = RecoveryEffects()
    resumer = service(store, Observer(observe), effects)
    with pytest.raises(ResumeError, match="terminal"):
        resumer.resume(37)
    assert not effects.calls
    with pytest.raises(ResumeError, match="checkpoint alcançado"):
        resumer.resume(37, recover_failed=True)
    assert store.get(run.id).phase == Phase.TESTING
    assert effects.calls == {"correction": 1}


def test_work_nao_seleciona_outra_issue_com_historico_orfao(tmp_path):
    from ai_dev_orchestrator.services.work import WorkService, WorkError
    from test_work import config
    store = SqliteExecutionStore(tmp_path / "state.db")
    historical(store)
    with pytest.raises(WorkError, match="reconciliação"):
        WorkService(config(tmp_path), store, object(), object(), object(), object(), object()).work()


def session_file(tmp_path, session="session", cwd=None):
    path = tmp_path / "sessions/2026/09/07" / f"rollout-2026-09-07T00-00-00-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": session, "cwd": str(cwd or tmp_path / "diretório com espaços")}}
    path.write_bytes(json.dumps(meta).encode() + b"\n" + b"conteudo posterior nao deve ser lido\xff\n")
    return path


def test_prova_sessao_le_apenas_metadados_e_respeita_codex_home(tmp_path, monkeypatch):
    path = session_file(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert session_matches_worktree("session", str(tmp_path / "diretório com espaços"))
    assert not session_matches_worktree("session", str(tmp_path / "other"))
    assert not session_matches_worktree("../session", str(tmp_path))
    assert not session_matches_worktree("unknown", str(tmp_path))
    path.write_bytes(b"{invalido\n")
    assert not session_matches_worktree("session", str(tmp_path), tmp_path)


def test_sessao_ambigua_e_metadados_grandes_bloqueiam(tmp_path):
    path = session_file(tmp_path)
    duplicate = path.with_name("rollout-2026-09-08T00-00-00-session.jsonl")
    duplicate.write_bytes(path.read_bytes())
    assert not session_matches_worktree("session", str(tmp_path / "diretório com espaços"), tmp_path)
    path = session_file(tmp_path / "other")
    path.write_bytes(b" " * 70000 + b"\n")
    assert not session_matches_worktree("session", str(tmp_path), tmp_path / "other")
