"""Recovery tipado, orçamento de retry e invariantes duráveis após reinício."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase as Phase
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind as Kind, FAILURE_POLICY
from ai_dev_orchestrator.domain.recovery import RecoveryObservation, WorktreeState, MergeObservation, MergeState
from ai_dev_orchestrator.domain.review import ReviewFinding, FindingSeverity, StructuredReview, ReviewVerdict
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore, ActiveExecutionError
from ai_dev_orchestrator.services.provider_recovery import (
    record_provider_failure, resume_provider_wait, ProviderRecoveryError,
)
from ai_dev_orchestrator.services.resume import ResumeError
from test_resume import advance, HEAD, OLD, pr, Observer, Effects, service


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def failure(kind, provider="codex", session="session"):
    return ProviderFailure(provider, kind, "token=segredo https://secret.invalid/webhook prompt privado",
                           NOW, session_id=session, returncode=126)


def running(store):
    run = advance(store, Phase.CODEX_RUNNING)
    return store.checkpoint(run.id, summary="sessão", codex_session_id="session")


def rejected(store):
    run = advance(store, Phase.WAITING_CI)
    store.transition(run.id, Phase.GEMINI_REVIEWING, summary="CI", ci_head_sha=HEAD)
    store.record_review(run.id, StructuredReview(ReviewVerdict.REJECTED,
                        (ReviewFinding(FindingSeverity.HIGH, "Corrigir", "Ajuste", "src/app.py", 2),), HEAD, "review"), "review")
    return store.transition(run.id, Phase.NEEDS_CHANGES, summary="correção necessária")


@pytest.mark.parametrize("kind", list(Kind))
def test_matriz_exaustiva_nao_orfana_execucao(tmp_path, kind):
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = running(store)
    assert set(FAILURE_POLICY) == set(Kind)
    run = record_provider_failure(store, original.id, failure(kind))
    if kind in {Kind.NETWORK_ERROR, Kind.TIMEOUT, Kind.LOCAL_TRANSIENT}:
        assert run.phase == Phase.WAITING_PROVIDER and run.quota_retry_at == NOW + timedelta(seconds=30)
    elif kind in {Kind.TERMINAL_QUOTA, Kind.TRANSIENT_RATE_LIMIT}:
        assert run.phase == Phase.WAITING_CODEX_QUOTA and run.quota_retry_at is None
    else:
        assert run.phase == Phase.BLOCKED_PROVIDER and run.quota_retry_at is None
    assert store.list_active() == (run,)
    assert run.codex_session_id == original.codex_session_id and run.current_head_sha == original.current_head_sha
    assert run.provider_resume_phase == "CODEX_RUNNING"
    assert "segredo" not in run.last_error and "https://" not in run.last_error and "privado" not in run.last_error
    assert kind.value in run.last_error and "126" in run.last_error
    with pytest.raises(ActiveExecutionError):
        store.create(original.issue_number)


def test_backoff_persistido_limitado_e_manual_explicito(tmp_path):
    path = tmp_path / "state.db"
    store = SqliteExecutionStore(path)
    run = running(store)
    for attempt, delay in enumerate((30, 60, 120), 1):
        run = record_provider_failure(store, run.id, failure(Kind.NETWORK_ERROR))
        assert run.provider_retry_attempts == attempt
        assert run.quota_retry_at == NOW + timedelta(seconds=delay)
        store = SqliteExecutionStore(path)
        assert resume_provider_wait(store, run, now=NOW) == run
        run = resume_provider_wait(store, run, now=NOW + timedelta(seconds=delay))
        assert run.phase == Phase.CODEX_RUNNING
    run = record_provider_failure(store, run.id, failure(Kind.NETWORK_ERROR))
    assert run.phase == Phase.BLOCKED_PROVIDER
    with pytest.raises(ProviderRecoveryError, match="intervenção"):
        resume_provider_wait(store, run)
    retried = resume_provider_wait(store, run, manual_retry=True)
    assert retried.id == run.id and retried.provider_retry_attempts == 0


@pytest.mark.parametrize("kind", [Kind.NETWORK_ERROR, Kind.TERMINAL_QUOTA, Kind.UNKNOWN])
def test_sessao_ausente_ou_divergente_bloqueia_sem_trocar(tmp_path, kind):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, Phase.CODEX_RUNNING)
    blocked = record_provider_failure(store, run.id, failure(kind, session=None))
    assert blocked.phase == Phase.BLOCKED_PROVIDER
    with pytest.raises(ProviderRecoveryError, match="Sessão Codex"):
        resume_provider_wait(store, blocked, manual_retry=True)
    assert len(store.list_history()) == 1


def test_sessao_divergente_preserva_original(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = running(store)
    result = record_provider_failure(store, run.id, failure(Kind.NETWORK_ERROR, session="other"))
    assert result.phase == Phase.BLOCKED_PROVIDER and result.codex_session_id == "session"
    assert result.quota_classification == "PROTOCOL_ERROR"


@pytest.mark.parametrize("kind", [Kind.NETWORK_ERROR, Kind.TIMEOUT, Kind.TERMINAL_QUOTA])
def test_recovery_needs_changes_nao_incrementa_correcao_no_retry(tmp_path, kind):
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = rejected(store)
    findings = store.review_findings(original.id, HEAD)
    class Interrupted(Effects):
        fail = True
        def resume_correction(self, run, received_findings):
            self.called("correction")
            assert received_findings == findings and run.codex_session_id == "session"
            if self.fail:
                raise failure(kind)
            return run.codex_session_id
    effects = Interrupted()
    snapshot = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
                                   pull_requests=(pr(),), findings_head_sha=HEAD, merge=MergeObservation(MergeState.OPEN))
    def observe(run):
        if run.phase == Phase.TESTING:
            raise RuntimeError("checkpoint alcançado")
        return snapshot
    first = service(store, Observer(observe), effects).resume(37)
    waiting = store.get(original.id)
    assert first.execution_id == original.id and waiting.provider_resume_phase == "CODEX_RUNNING"
    assert waiting.correction_attempts == 1 and waiting.reviewed_head_sha == HEAD
    assert waiting.pull_request_number == original.pull_request_number
    effects.fail = False
    reopened = SqliteExecutionStore(store.database_path)
    with pytest.raises(ResumeError, match="checkpoint alcançado"):
        service(reopened, Observer(observe), effects).resume(37, retry_provider=kind == Kind.TERMINAL_QUOTA)
    final = reopened.get(original.id)
    assert final.phase == Phase.TESTING and final.correction_attempts == 1
    assert final.current_head_sha == HEAD and final.codex_session_id == "session"
    assert reopened.review_findings(original.id, HEAD) == findings
    assert effects.calls == {"correction": 2} and len(reopened.list_history()) == 1


def test_network_em_codex_running_retorna_mesma_sessao(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = running(store)
    class Interrupted(Effects):
        def resume_codex(self, current):
            assert current.id == run.id and current.codex_session_id == "session"
            raise failure(Kind.NETWORK_ERROR)
    observed = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=OLD)
    result = service(store, Observer(lambda _: observed), Interrupted()).resume(37)
    assert result.phase == "WAITING_PROVIDER" and result.codex_session_id == "session"


def test_timeout_gate_local_retomado_na_mesma_fase(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    original = advance(store, Phase.TESTING)
    class Interrupted(Effects):
        def run_local_gates(self, run):
            raise failure(Kind.TIMEOUT, "local", session=None)
    snapshot = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=OLD)
    result = service(store, Observer(lambda _: snapshot), Interrupted()).resume(37)
    run = store.get(original.id)
    assert result.phase == "WAITING_PROVIDER" and run.provider_resume_phase == "TESTING"
    resumed = resume_provider_wait(store, run)
    assert resumed.phase == Phase.TESTING and resumed.codex_session_id == original.codex_session_id


def test_network_em_review_nunca_libera_merge(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, Phase.WAITING_CI)
    store.transition(run.id, Phase.GEMINI_REVIEWING, summary="ci", ci_head_sha=HEAD)
    class Interrupted(Effects):
        def review_head(self, run, findings):
            self.called("review")
            raise failure(Kind.NETWORK_ERROR, "gemini", None)
    effects = Interrupted()
    observed = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD, pull_requests=(pr(),))
    result = service(store, Observer(lambda _: observed), effects).resume(37)
    assert result.phase == "WAITING_PROVIDER" and effects.calls == {"review": 1}
    persisted = store.get(run.id)
    assert persisted.review_verdict is None and persisted.merge_commit_sha is None
    assert persisted.provider_resume_phase == "GEMINI_REVIEWING"


def test_crash_primeira_chamada_sem_id_nao_cria_segunda_sessao(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, Phase.CODEX_RUNNING)
    store.checkpoint(run.id, summary="chamada iniciada", codex_start_attempted=True)
    effects = Effects()
    with pytest.raises(ResumeError, match="sem sessão comprovada"):
        service(store, Observer(lambda _: RecoveryObservation(WorktreeState.CONVERGENT)), effects).resume(37)
    assert effects.calls == {} and store.get(run.id).id == run.id


def test_retry_passado_ou_sem_timezone_nao_autoriza_loop(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = running(store)
    stale = replace(failure(Kind.TERMINAL_QUOTA), retry_at=NOW - timedelta(seconds=1))
    run = record_provider_failure(store, run.id, stale)
    assert run.quota_retry_at is None
    with pytest.raises(ProviderRecoveryError, match="intervenção"):
        resume_provider_wait(store, run)


@pytest.mark.parametrize("change", [{"remote_head_sha": "other"}, {"local_head_sha": "other"},
                                     {"merge": MergeObservation(MergeState.MERGED)}])
def test_divergencia_antes_retry_correcao_bloqueia(tmp_path, change):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = rejected(store)
    store.transition(run.id, Phase.CODEX_RUNNING, summary="corrigindo", correction_attempts=1)
    waiting = record_provider_failure(store, run.id, failure(Kind.NETWORK_ERROR))
    snapshot = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
                                   pull_requests=(pr(),), findings_head_sha=HEAD, merge=MergeObservation(MergeState.OPEN))
    effects = Effects()
    with pytest.raises(ResumeError, match="divergiu"):
        service(store, Observer(lambda _: replace(snapshot, **change)), effects).resume(37)
    assert effects.calls == {} and store.get(waiting.id).current_head_sha == HEAD


def test_supervisor_atravessa_backoff_e_para_no_limite(tmp_path, monkeypatch):
    from ai_dev_orchestrator.services import supervisor as supervisor_module
    from ai_dev_orchestrator.services.supervisor import SupervisorService, SupervisorError
    from ai_dev_orchestrator.services.pipeline import RunPipelineError
    from test_work import config
    cfg = config(tmp_path)
    store = SqliteExecutionStore(cfg.state.database_path)
    run = running(store)
    class Clock:
        current = NOW
        @classmethod
        def now(cls, tz):
            return cls.current
    monkeypatch.setattr(supervisor_module, "datetime", Clock)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        Clock.current += timedelta(seconds=seconds)
    class Work:
        calls = 0
        def work(self):
            self.calls += 1
            current = resume_provider_wait(store, store.get(run.id), now=Clock.current)
            record_provider_failure(store, current.id, replace(failure(Kind.NETWORK_ERROR), observed_at=Clock.current))
            raise RunPipelineError("falha checkpointada")
    work = Work()
    with pytest.raises(SupervisorError, match="intervenção"):
        SupervisorService(cfg, work, store, sleep).watch()
    assert sleeps == [30, 60, 120] and work.calls == 4
    assert store.get(run.id).phase == Phase.HUMAN_REQUIRED
    assert len(store.list_history()) == 1
    assert not cfg.state.database_path.with_suffix(".watch.lock").exists()


def test_politica_de_quota_nao_contorna_bloqueio_auth(tmp_path):
    from ai_dev_orchestrator.services.supervisor import SupervisorService, SupervisorError
    from test_work import config
    cfg = config(tmp_path)
    cfg.supervisor.retry_without_reset_seconds = 1
    store = SqliteExecutionStore(cfg.state.database_path)
    run = running(store)
    record_provider_failure(store, run.id, failure(Kind.AUTH_ERROR))
    with pytest.raises(SupervisorError, match="intervenção"):
        SupervisorService(cfg, object(), store, lambda _: pytest.fail("não deve dormir")).watch()
