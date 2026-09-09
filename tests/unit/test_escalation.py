"""Escalonamento e notificações com providers locais, sem rede real."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from ai_dev_orchestrator.config import NotificationConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase as Phase
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind as Kind
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.escalation import EscalationService
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.provider_recovery import record_provider_failure, resume_provider_wait
from ai_dev_orchestrator.services.resume import ResumeError
from ai_dev_orchestrator.services.supervisor import SupervisorError, SupervisorService
from test_issue_44 import _config
from test_resume import advance, service, Observer, Effects, HEAD
from test_review_loop import LoopFakes, pipeline


@pytest.fixture
def setup(tmp_path):
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    channels = {name: Mock() for name in ("email", "discord", "telegram")}
    project = Mock()
    escalation = EscalationService(config, store, project, channels)
    return config, store, channels, project, escalation


@pytest.mark.parametrize("maximum", [1, 5])
def test_limite_real_do_pipeline_notifica_e_preserva_identidade(tmp_path, monkeypatch, maximum):
    channels = []
    monkeypatch.setattr("ai_dev_orchestrator.adapters.notifications.EnvironmentNotificationAdapter.send", lambda self, text: channels.append(text))
    fakes = LoopFakes(rejected_reviews=maximum + 1)
    runner = pipeline(tmp_path, fakes, maximum=maximum)
    runner.config.notifications.channels = ("email",)
    runner.execution_store = SqliteExecutionStore(tmp_path / "state.db")
    with pytest.raises(RunPipelineError, match="Limite"):
        runner.run(31, "feat/review-loop")
    run = runner.execution_store.get_latest_for_issue(31)
    assert run.phase == Phase.HUMAN_REQUIRED
    assert run.human_reason == "CORRECTION_LIMIT"
    assert run.human_phase == "NEEDS_CHANGES"
    assert run.codex_session_id == "sessao-original"
    assert run.pull_request_number == 32 and run.correction_attempts == maximum
    assert run.current_head_sha == run.reviewed_head_sha == run.ci_head_sha
    assert run.project_status == "Human Review" and run.human_at
    assert len(channels) == 1 and "HUMAN_REQUIRED" in channels[0]
    assert f"Correções: {maximum}" in channels[0] and "PR: 32" in channels[0]
    assert fakes.events.count("execute") == fakes.events.count("criar-pr") == 1


@pytest.mark.parametrize("kind", [Kind.AUTH_ERROR, Kind.MODEL_UNAVAILABLE])
def test_provider_bloqueado_notifica_e_permite_retry_mesma_sessao(setup, kind):
    _, store, channels, project, escalation = setup
    original = advance(store, Phase.CODEX_RUNNING)
    original = store.checkpoint(original.id, summary="Sessão comprovada", codex_session_id="session")
    blocked = record_provider_failure(store, original.id, ProviderFailure("codex", kind, "diagnóstico privado", datetime.now(timezone.utc), session_id="session"))
    human = escalation.assess(blocked)
    assert human.phase == Phase.HUMAN_REQUIRED and human.human_reason == kind.value
    assert human.quota_classification == kind.value
    assert human.human_phase == "CODEX_RUNNING"
    for channel in channels.values():
        channel.send.assert_called_once()
    project.set_status.assert_called_once_with(original.project_item_id, "Blocked")
    resumed = resume_provider_wait(store, human, manual_retry=True)
    assert resumed.phase == Phase.CODEX_RUNNING
    assert (resumed.id, resumed.codex_session_id, resumed.current_head_sha, resumed.branch) == (original.id, original.codex_session_id, original.current_head_sha, original.branch)


def test_quota_com_retry_conhecido_nao_notifica(setup):
    _, store, channels, project, escalation = setup
    run = advance(store, Phase.CODEX_RUNNING)
    now = datetime.now(timezone.utc)
    waiting = record_provider_failure(store, run.id, ProviderFailure("codex", Kind.TERMINAL_QUOTA, "quota", now, retry_at=now + timedelta(hours=1), session_id="session"))
    assert escalation.assess(waiting) == waiting
    for channel in channels.values():
        channel.send.assert_not_called()
    project.set_status.assert_not_called()


def test_quota_sem_retry_respeita_politica_local(setup):
    config, store, channels, _, escalation = setup
    run = advance(store, Phase.CODEX_RUNNING)
    waiting = record_provider_failure(store, run.id, ProviderFailure("codex", Kind.TERMINAL_QUOTA, "quota", datetime.now(timezone.utc), session_id="session"))
    config.supervisor.retry_without_reset_seconds = 60
    assert escalation.assess(waiting) == waiting
    channels["email"].send.assert_not_called()
    config.supervisor.retry_without_reset_seconds = None
    assert escalation.assess(waiting).human_reason == "QUOTA_NO_RETRY"
    channels["email"].send.assert_called_once()


def test_deduplicacao_persistida_e_mudanca_de_causa(setup):
    config, store, channels, project, escalation = setup
    run = advance(store, Phase.WAITING_CI)
    original = run
    for _ in range(3):
        escalation.escalate(store.get(run.id), "REMOTE_AMBIGUOUS")
    restarted = EscalationService(config, SqliteExecutionStore(store.database_path), project, channels)
    restarted.assess(store.get(run.id))
    channels["email"].send.assert_called_once()
    restarted.escalate(store.get(run.id), "CI_TERMINAL")
    assert channels["email"].send.call_count == 2
    run = store.get(run.id)
    assert (run.id, run.codex_session_id, run.pull_request_number, run.current_head_sha, run.branch, run.worktree_path) == (original.id, original.codex_session_id, original.pull_request_number, original.current_head_sha, original.branch, original.worktree_path)
    assert len(store.list_history()) == 1


def test_falha_de_email_e_project_nao_impede_demais_canais_e_nao_vaza_segredos(setup, monkeypatch, capsys):
    _, store, channels, project, escalation = setup
    secret = "credencial-ultrassecreta"
    monkeypatch.setenv("ORCH_SMTP_PASSWORD", secret)
    channels["email"].send.side_effect = RuntimeError(secret)
    project.set_status.side_effect = RuntimeError(secret)
    run = advance(store, Phase.WAITING_CI)
    run = escalation.escalate(run, "CI_TERMINAL")
    assert run.phase == Phase.HUMAN_REQUIRED
    channels["discord"].send.assert_called_once()
    channels["telegram"].send.assert_called_once()
    rows = store.notification_deliveries(run.id)
    assert {row["channel"]: row["status"] for row in rows} == {"email": "FAILED", "project": "FAILED", "discord": "SENT", "telegram": "SENT"}
    with store._connection() as connection:
        dump = "\n".join(connection.iterdump())
    assert secret not in dump + str(channels) + capsys.readouterr().out
    assert secret not in channels["discord"].send.call_args.args[0]


def test_retry_entrega_nao_repete_canais_bem_sucedidos(setup):
    config, store, channels, project, escalation = setup
    run = advance(store, Phase.WAITING_CI)
    channels["email"].send.side_effect = RuntimeError("SMTP indisponível")
    run = escalation.escalate(run, "CI_TERMINAL")
    escalation.deliver(run)
    assert channels["email"].send.call_count == 1
    for _ in range(config.notifications.max_attempts + 1):
        with store._connection() as connection:
            connection.execute("UPDATE notification_deliveries SET attempted_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        escalation.deliver(store.get(run.id))
    assert channels["email"].send.call_count == config.notifications.max_attempts
    channels["discord"].send.assert_called_once()
    channels["telegram"].send.assert_called_once()
    project.set_status.assert_called_once()


def test_recovery_divergente_escala_sem_executar_efeitos(setup):
    from ai_dev_orchestrator.domain.recovery import RecoveryObservation, WorktreeState
    _, store, channels, _, escalation = setup
    original = advance(store, Phase.WAITING_CI)
    effects = Effects()
    resume = service(store, Observer(lambda _: RecoveryObservation(WorktreeState.DIVERGENT)), effects)
    resume.escalation = escalation
    with pytest.raises(ResumeError):
        resume.resume(original.issue_number)
    assert store.get(original.id).human_reason == "REMOTE_AMBIGUOUS"
    assert not effects.calls
    channels["email"].send.assert_called_once()


@pytest.mark.parametrize("pending", [False, True])
def test_ci_terminal_ou_timeout_do_gate_notifica(setup, pending):
    from ai_dev_orchestrator.domain.ci import PullRequestCiSnapshot, StatusCheck
    from ai_dev_orchestrator.domain.recovery import RecoveryObservation, WorktreeState, CiObservation, CiState
    from ai_dev_orchestrator.services.ci_gate import CiGate
    from test_resume import pr
    config, store, channels, _, escalation = setup
    run = advance(store, Phase.WAITING_CI)
    observation = RecoveryObservation(WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD, pull_requests=(pr(),), ci=CiObservation(CiState.PENDING, HEAD))
    clock = iter([0, 0, 10000])
    reader = Mock()
    reader.get_ci_snapshot.return_value = PullRequestCiSnapshot(HEAD, (StatusCheck("test", "IN_PROGRESS" if pending else "COMPLETED", None if pending else "FAILURE"),))
    effects = Effects()
    effects.wait_for_ci = lambda current: CiGate(reader, config.ci, monotonic=lambda: next(clock), sleep=lambda _: None).wait(current.pull_request_number, HEAD)
    resume = service(store, Observer(lambda _: observation), effects)
    resume.escalation = escalation
    with pytest.raises(ResumeError):
        resume.resume(run.issue_number)
    assert store.get(run.id).human_reason == "CI_TERMINAL"
    channels["email"].send.assert_called_once()


def test_watch_humano_repetido_nao_chama_pipeline(setup, monkeypatch):
    config, store, channels, project, escalation = setup
    run = escalation.escalate(advance(store, Phase.WAITING_CI), "CI_TERMINAL")
    monkeypatch.setattr("ai_dev_orchestrator.adapters.github.GitHubProjectStatusAdapter", lambda _: project)
    monkeypatch.setattr("ai_dev_orchestrator.services.escalation.EnvironmentNotificationAdapter", lambda name, timeout: channels[name])
    config.notifications.channels = tuple(channels)
    work = Mock()
    for _ in range(3):
        with pytest.raises(SupervisorError, match="HUMAN_REQUIRED"):
            SupervisorService(config, work, store, escalation=escalation).watch()
    work.work.assert_not_called()
    channels["email"].send.assert_called_once()
    assert store.get(run.id).phase == Phase.HUMAN_REQUIRED


def test_sem_canais_persiste_estado_e_recusa_segredos_na_config(setup):
    config, store, _, project, _ = setup
    escalation = EscalationService(config, store, project)
    run = escalation.escalate(advance(store, Phase.CODEX_RUNNING), "INTERNAL_ERROR")
    assert run.phase == Phase.HUMAN_REQUIRED
    assert {row["channel"] for row in store.notification_deliveries(run.id)} == {"project"}
    with pytest.raises(ValueError):
        NotificationConfig(password="não pode persistir")


@pytest.mark.parametrize("kind", [Kind.AUTH_ERROR, Kind.MODEL_UNAVAILABLE])
def test_pipeline_inicial_escala_provider_sem_nova_sessao(tmp_path, monkeypatch, kind):
    notified = Mock()
    monkeypatch.setattr("ai_dev_orchestrator.services.escalation.EnvironmentNotificationAdapter", lambda *args: notified)
    fakes = LoopFakes()
    fakes.execute = Mock(side_effect=ProviderFailure("codex", kind, "stdout privado", datetime.now(timezone.utc), session_id="session"))
    runner = pipeline(tmp_path, fakes)
    runner.execution_store = SqliteExecutionStore(tmp_path / "state.db")
    runner.config.notifications.channels = ("email",)
    with pytest.raises(RunPipelineError):
        runner.run(31, "feat/review-loop")
    run = runner.execution_store.get_latest_for_issue(31)
    assert run.phase == Phase.HUMAN_REQUIRED and run.human_reason == kind.value
    assert run.codex_session_id == "session"
    assert "stdout privado" not in str(run)
    notified.send.assert_called_once()
    fakes.execute.assert_called_once()


def test_merge_sem_convergencia_notifica_sem_repetir_merge(tmp_path, monkeypatch):
    notified = Mock()
    monkeypatch.setattr("ai_dev_orchestrator.services.escalation.EnvironmentNotificationAdapter", lambda *args: notified)
    fakes = LoopFakes(rejected_reviews=0, stale_merge_reads_after_merge=100)
    runner = pipeline(tmp_path, fakes, auto_merge=True)
    from ai_dev_orchestrator.services.convergence import ConvergencePoller
    clock = iter([0, 0, 10000])
    # A convergência inicial do PR usa o primeiro valor; o merge expira depois.
    runner.convergence = ConvergencePoller(runner.config.convergence, monotonic=lambda: next(clock, 10000), sleep=lambda _: None)
    runner.execution_store = SqliteExecutionStore(tmp_path / "state.db")
    runner.config.notifications.channels = ("email",)
    with pytest.raises(RunPipelineError):
        runner.run(31, "feat/review-loop")
    run = runner.execution_store.get_latest_for_issue(31)
    assert run.human_reason == "MERGE_BLOCKED"
    assert run.review_verdict == "APPROVED" and run.pull_request_number == 32
    assert len(fakes.merge_calls) == 1
    notified.send.assert_called_once()


def test_reserva_concorrente_envia_uma_unica_vez(setup):
    from concurrent.futures import ThreadPoolExecutor
    config, store, channels, project, _ = setup
    run = advance(store, Phase.WAITING_CI)
    def emit(_):
        fresh = SqliteExecutionStore(store.database_path)
        EscalationService(config, fresh, project, channels).escalate(run, "CI_TERMINAL")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(emit, range(2)))
    channels["email"].send.assert_called_once()
    assert len([event for event in store.events(run.id) if event.phase == Phase.HUMAN_REQUIRED and event.previous_phase != Phase.HUMAN_REQUIRED]) == 1


def test_todos_canais_falham_sem_apagar_estado_ou_project(setup):
    _, store, channels, project, escalation = setup
    for channel in channels.values():
        channel.send.side_effect = RuntimeError("Canal indisponível")
    run = escalation.escalate(advance(store, Phase.WAITING_CI), "CI_TERMINAL")
    assert run.phase == Phase.HUMAN_REQUIRED and run.project_status == "Blocked"
    project.set_status.assert_called_once()
    for channel in channels.values():
        channel.send.assert_called_once()


def test_retry_curto_nao_rouba_reserva_em_andamento(setup):
    _, store, _, _, _ = setup
    run = advance(store, Phase.WAITING_CI)
    assert store.claim_notification(run.id, "evento", "email", max_attempts=3, retry_seconds=1)
    with store._connection() as connection:
        connection.execute("UPDATE notification_deliveries SET attempted_at = ?", ((datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),))
    assert not store.claim_notification(run.id, "evento", "email", max_attempts=3, retry_seconds=1)
