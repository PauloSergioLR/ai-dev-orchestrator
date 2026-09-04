"""Escalonamento humano e notificações sem acesso à rede."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.notification import HumanRequiredNotification
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
from ai_dev_orchestrator.domain.recovery import RecoveryAction, RecoveryDecision
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.escalation import HumanEscalationService
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunPipelineError
from ai_dev_orchestrator.services.resume import ResumeService


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={
            "owner": "acme",
            "repository": "repo",
            "project_number": 1,
            "ready_status": "Ready",
            "human_required_status": "Human Review",
        },
        workspace={
            "repository_path": tmp_path / "repo",
            "worktrees_dir": tmp_path / "worktrees",
            "base_branch": "main",
        },
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )


@dataclass
class Channel:
    name: str
    fail: bool = False
    events: list[HumanRequiredNotification] = field(default_factory=list)

    def send(self, event: HumanRequiredNotification) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("password=segredo-da-entrega")


@dataclass
class StatusWriter:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def set_status(self, item: str, status: str) -> None:
        self.calls.append((item, status))


def _run(store: SqliteExecutionStore):
    run = store.create(
        45,
        project_item_id="ITEM",
        branch="work/escalonamento",
        worktree_path="C:/worktree",
        base_ref="main",
    )
    return store.checkpoint(
        run.id,
        summary="identidade",
        codex_session_id="sessao-original",
        pull_request_number=10,
        pull_request_url="https://github.com/acme/repo/pull/10",
        current_head_sha="a" * 40,
        correction_attempts=5,
    )


def test_escalation_persists_context_updates_project_and_deduplicates(tmp_path: Path) -> None:
    config, store = _config(tmp_path), SqliteExecutionStore(tmp_path / "state.db")
    run = _run(store)
    channel, writer = Channel("email"), StatusWriter()
    service = HumanEscalationService(config, store, writer, (channel,))

    first = service.escalate(
        run.id,
        "CORRECTION_LIMIT_REACHED",
        "Limite de cinco correções atingido.",
        suggested_action="Revise os findings.",
    )
    second = service.escalate(
        run.id,
        "CORRECTION_LIMIT_REACHED",
        "Limite de cinco correções atingido.",
    )

    assert first.phase == second.phase == ExecutionPhase.HUMAN_REQUIRED
    assert first.blocked_phase == "PREPARING"
    assert first.codex_session_id == "sessao-original"
    assert first.pull_request_number == 10
    assert first.current_head_sha == "a" * 40
    assert first.correction_attempts == 5
    assert writer.calls == [("ITEM", "Human Review")]
    assert len(channel.events) == 1
    assert store.notification_deliveries(run.id)[0]["status"] == "SENT"


def test_new_cause_notifies_again_and_channel_failure_does_not_stop_others(tmp_path: Path) -> None:
    config, store = _config(tmp_path), SqliteExecutionStore(tmp_path / "state.db")
    run = _run(store)
    email, discord, telegram = Channel("email", fail=True), Channel("discord"), Channel("telegram")
    service = HumanEscalationService(config, store, channels=(email, discord, telegram))

    service.escalate(run.id, "AUTH_ERROR", "Autenticação recusada.")
    service.escalate(run.id, "MODEL_UNAVAILABLE", "Modelo indisponível.")

    assert len(email.events) == len(discord.events) == len(telegram.events) == 2
    deliveries = store.notification_deliveries(run.id)
    assert {row["status"] for row in deliveries} == {"FAILED", "SENT"}
    assert all("segredo-da-entrega" not in str(row) for row in deliveries)


def test_explicit_delivery_retry_does_not_repeat_project_mutation(tmp_path: Path) -> None:
    config, store = _config(tmp_path), SqliteExecutionStore(tmp_path / "state.db")
    run = _run(store)
    channel, writer = Channel("email", fail=True), StatusWriter()
    service = HumanEscalationService(config, store, writer, (channel,))
    service.escalate(run.id, "CI_TERMINAL_FAILURE", "CI falhou.")
    channel.fail = False

    service.retry_failed_notifications(run.id)

    assert len(channel.events) == 2
    assert writer.calls == [("ITEM", "Human Review")]
    delivery = store.notification_deliveries(run.id)[0]
    assert delivery["status"] == "SENT"
    assert delivery["attempts"] == 2


def test_provider_auth_escalates_and_recoverable_quota_does_not(tmp_path: Path) -> None:
    config, store = _config(tmp_path), SqliteExecutionStore(tmp_path / "state.db")
    run = _run(store)
    channel = Channel("discord")
    escalation = HumanEscalationService(config, store, channels=(channel,))
    pipeline = RunPipeline(
        config, object(), object(), object(), object(), object(),
        execution_store=store, escalator=escalation,
    )
    pipeline._execution_id = run.id

    try:
        pipeline._record_provider_wait(
            ProviderFailure(
                "codex", ProviderFailureKind.AUTH_ERROR, "login required",
                datetime.now(timezone.utc),
            ),
            ExecutionPhase.WAITING_CODEX_QUOTA,
        )
    except RunPipelineError:
        pass

    assert store.get(run.id).phase == ExecutionPhase.HUMAN_REQUIRED
    assert channel.events[0].failure_classification == "AUTH_ERROR"

    other_store = SqliteExecutionStore(tmp_path / "quota.db")
    waiting = other_store.create(46, branch="work/quota", worktree_path="C:/quota", base_ref="main")
    waiting = other_store.transition(waiting.id, ExecutionPhase.CODEX_RUNNING, summary="codex")
    other_store.transition(
        waiting.id,
        ExecutionPhase.WAITING_CODEX_QUOTA,
        summary="quota",
        quota_retry_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    quota_channel = Channel("email")
    quota_escalation = HumanEscalationService(config, other_store, channels=(quota_channel,))

    class Never:
        def __getattr__(self, _name: str):
            raise AssertionError("recovery não deveria executar")

    result = ResumeService(other_store, Never(), Never(), Never(), escalator=quota_escalation).resume(46)
    assert result.phase == "WAITING_CODEX_QUOTA"
    assert quota_channel.events == []


def test_configured_secret_is_redacted_from_sqlite_and_message(tmp_path: Path, monkeypatch) -> None:
    config, store = _config(tmp_path), SqliteExecutionStore(tmp_path / "state.db")
    run = _run(store)
    channel = Channel("telegram")
    secret = "token-super-secreto"
    monkeypatch.setenv(config.notifications.telegram_token_env, secret)

    HumanEscalationService(config, store, channels=(channel,)).escalate(
        run.id, "REMOTE_STATE_AMBIGUOUS", f"Resposta continha {secret}."
    )

    persisted = store.get(run.id)
    assert secret not in (persisted.human_reason or "")
    assert secret not in channel.events[0].message()


def test_recovery_divergence_and_stuck_ci_become_human_required(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class Observer:
        def observe(self, _run):
            return "observação"

    class Executor:
        def execute(self, *_args):
            raise AssertionError("efeito de recovery não deveria executar")

    class Planner:
        def __init__(self, decision: RecoveryDecision) -> None:
            self.decision = decision

        def plan(self, *_args):
            return self.decision

    divergent_store = SqliteExecutionStore(tmp_path / "divergent.db")
    divergent = _run(divergent_store)
    divergence_channel = Channel("discord")
    divergence_escalation = HumanEscalationService(
        config, divergent_store, channels=(divergence_channel,)
    )
    result = ResumeService(
        divergent_store,
        Observer(),
        Planner(RecoveryDecision(RecoveryAction.BLOCK, "HEAD remoto divergente.")),
        Executor(),
        escalator=divergence_escalation,
    ).resume(divergent.issue_number)

    assert result.execution_id == divergent.id
    assert result.phase == "HUMAN_REQUIRED"
    assert divergence_channel.events[0].reason_code == "GIT_PR_HEAD_DIVERGENCE"

    ci_store = SqliteExecutionStore(tmp_path / "ci.db")
    ci = ci_store.create(
        47, branch="work/ci", worktree_path="C:/ci", base_ref="main"
    )
    for phase in (
        ExecutionPhase.CODEX_RUNNING,
        ExecutionPhase.TESTING,
        ExecutionPhase.COMMIT_PENDING,
        ExecutionPhase.PUSH_PENDING,
        ExecutionPhase.PR_PENDING,
        ExecutionPhase.WAITING_CI,
    ):
        ci = ci_store.transition(ci.id, phase, summary="avanço")
    ci_channel = Channel("telegram")
    ci_escalation = HumanEscalationService(config, ci_store, channels=(ci_channel,))
    result = ResumeService(
        ci_store,
        Observer(),
        Planner(RecoveryDecision(RecoveryAction.WAIT_FOR_CI, "CI pendente.")),
        Executor(),
        escalator=ci_escalation,
        ci_timeout_seconds=0,
    ).resume(ci.issue_number)

    assert result.phase == "HUMAN_REQUIRED"
    assert ci_channel.events[0].reason_code == "CI_STUCK_TIMEOUT"
