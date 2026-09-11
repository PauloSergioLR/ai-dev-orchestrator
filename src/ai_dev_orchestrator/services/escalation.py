"""Escalonamento durável e entrega independente de efeitos do pipeline."""

from hashlib import sha256
from collections.abc import Callable, Mapping
import os
import re
from typing import Protocol

from ai_dev_orchestrator.adapters.notifications import (
    DiscordWebhookProvider,
    EnvironmentNotificationAdapter,
    TelegramBotProvider,
)
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, ExecutionStore, RunRecord, PROVIDER_WAIT_PHASES, TERMINAL_PHASES


class NotificationChannel(Protocol):
    def send(self, message: str) -> None: ...


class ProjectStatusWriter(Protocol):
    def set_status(self, project_item_id: str, status_name: str) -> None: ...


REASONS = {
    "CORRECTION_LIMIT": "Limite configurado de correções atingido",
    "AUTH_ERROR": "Autenticação do provider exige correção",
    "MODEL_UNAVAILABLE": "Modelo indisponível sem recuperação automática",
    "PROVIDER_BLOCKED": "Provider não pode continuar automaticamente",
    "QUOTA_NO_RETRY": "Quota sem instante confiável ou política segura de retry",
    "CI_TERMINAL": "CI falhou ou excedeu o timeout configurado",
    "MERGE_BLOCKED": "Merge não convergiu após aprovação",
    "REMOTE_AMBIGUOUS": "Estado Git/PR/HEAD ambíguo ou divergente",
    "INTERNAL_ERROR": "Erro interno impediu a continuidade segura",
}

NOTIFIABLE_PHASES = frozenset({
    ExecutionPhase.HUMAN_REQUIRED,
    ExecutionPhase.WAITING_CODEX_QUOTA,
    ExecutionPhase.FAILED,
    ExecutionPhase.NEEDS_CHANGES,
    ExecutionPhase.COMPLETED,
})


def safe_context(value: object) -> str:
    text = str(value or "-").replace("\n", " ").replace("\r", " ")
    for name, secret in os.environ.items():
        if any(part in name.upper() for part in ("TOKEN", "PASSWORD", "SECRET", "WEBHOOK")) and secret:
            text = text.replace(secret, "[redigido]")
    return re.sub(r"https?://\S+", "[URL omitida]", text)[:150]


class EscalationService:
    def __init__(self, config: OrchestratorConfig, store: ExecutionStore,
                 status_writer: ProjectStatusWriter | None = None,
                 channels: Mapping[str, NotificationChannel] | None = None) -> None:
        self.config, self.store, self.status_writer = config, store, status_writer
        if channels is not None:
            self.channels = channels
        else:
            providers: dict[str, NotificationChannel] = {}
            for name in config.notifications.channels:
                if name == "discord" and config.notifications.discord_enabled:
                    providers[name] = DiscordWebhookProvider(config.notifications.timeout_seconds)
                elif name == "telegram" and config.notifications.telegram_enabled:
                    providers[name] = TelegramBotProvider(config.notifications.timeout_seconds)
                elif name == "email":
                    providers[name] = EnvironmentNotificationAdapter(name, config.notifications.timeout_seconds)
            self.channels = providers

    def assess(self, run: RunRecord, *, error: Exception | None = None) -> RunRecord:
        if run.phase in TERMINAL_PHASES:
            self.deliver_event(run)
            return run
        if run.phase == ExecutionPhase.HUMAN_REQUIRED:
            self.deliver(run)
            return self.store.get(run.id)
        if run.phase == ExecutionPhase.BLOCKED_PROVIDER:
            reason = run.quota_classification
            return self.escalate(run, reason if reason in REASONS else "PROVIDER_BLOCKED")
        if run.phase in PROVIDER_WAIT_PHASES:
            self.deliver_event(run)
            if run.quota_retry_at is not None:
                return run
            if (run.phase != ExecutionPhase.WAITING_PROVIDER
                    and self.config.supervisor.retry_without_reset_seconds is not None
                    and run.quota_observed_at is not None):
                return run
            return self.escalate(run, "QUOTA_NO_RETRY")
        cause = error
        while cause is not None:
            reason = getattr(cause, "reason", None)
            if reason in REASONS:
                return self.escalate(run, reason)
            cause = cause.__cause__
        if run.correction_attempts >= self.config.review.max_correction_attempts and run.review_verdict == "REJECTED":
            return self.escalate(run, "CORRECTION_LIMIT")
        reason = {
            ExecutionPhase.WAITING_CI: "CI_TERMINAL",
            ExecutionPhase.MERGING: "MERGE_BLOCKED",
            ExecutionPhase.MERGE_PENDING: "MERGE_BLOCKED",
            ExecutionPhase.PR_PENDING: "REMOTE_AMBIGUOUS",
            ExecutionPhase.PUSH_PENDING: "REMOTE_AMBIGUOUS",
        }.get(run.phase, "INTERNAL_ERROR")
        return self.escalate(run, reason)

    def escalate(self, run: RunRecord, reason: str) -> RunRecord:
        reason = reason if reason in REASONS else "REMOTE_AMBIGUOUS"
        run = self.store.require_human(run.id, summary=REASONS[reason], reason=reason)
        self.deliver(run)
        return self.store.get(run.id)

    def deliver(self, run: RunRecord) -> None:
        self.deliver_event(run)
        if run.phase != ExecutionPhase.HUMAN_REQUIRED:
            return
        status = "Human Review" if run.human_reason == "CORRECTION_LIMIT" else "Blocked"
        key = sha256(f"{run.human_reason}:{run.human_phase}:{run.current_head_sha}:{run.correction_attempts}".encode()).hexdigest()
        if self.status_writer and run.project_item_id:
            self._attempt(run, key, "project", lambda: self._project(run, status))
        message = (
            f"{safe_context(self.config.github.repository_full_name)} | Issue #{run.issue_number} | HUMAN_REQUIRED\n"
            f"Motivo: {REASONS.get(run.human_reason, REASONS['REMOTE_AMBIGUOUS'])}\n"
            f"Fase: {safe_context(run.human_phase)} | PR: {run.pull_request_number or '-'} | HEAD: {safe_context(run.current_head_sha)}\n"
            f"Correções: {run.correction_attempts} | Horário: {run.human_at}\n"
            f"Ação: inspecione orch state --issue {run.issue_number} e corrija a causa antes de retomar."
        )
        if (
            self.config.notifications.enabled
            and ExecutionPhase.HUMAN_REQUIRED.value in self.config.notifications.events
        ):
            for name, channel in self.channels.items():
                self._attempt(run, key, name, lambda channel=channel: channel.send(message))

    def deliver_event(self, run: RunRecord) -> None:
        """Despacha uma transição relevante uma vez por contexto material."""
        policy = self.config.notifications
        if not policy.enabled or run.phase not in NOTIFIABLE_PHASES or run.phase.value not in policy.events:
            return
        if run.phase == ExecutionPhase.HUMAN_REQUIRED:
            # Mantém o formato operacional já usado por intervenções humanas.
            return
        provider = run.quota_provider or ("gemini" if run.phase.value.startswith("WAITING_GEMINI") else "codex")
        reason = run.last_error or run.quota_classification or run.review_verdict or "Transição registrada"
        key = sha256(
            f"{run.phase}:{run.current_head_sha}:{run.pull_request_number}:{reason}:{run.correction_attempts}".encode()
        ).hexdigest()
        timestamp = run.updated_at.isoformat()
        issue_url = (
            f"https://github.com/{self.config.github.repository_full_name}/issues/{run.issue_number}"
        )
        links = f"Issue: {issue_url}"
        if run.pull_request_url:
            links += f" | PR: {run.pull_request_url}"
        message = (
            f"{safe_context(self.config.github.repository_full_name)} | Issue #{run.issue_number} | {run.phase.value}\n"
            f"Resumo: {safe_context(reason)}\n"
            f"Branch: {safe_context(run.branch)} | Provider: {safe_context(provider)} | PR: {run.pull_request_number or '-'}\n"
            f"Horário: {timestamp}\n{links}"
        )
        for name, channel in self.channels.items():
            self._attempt(run, key, name, lambda channel=channel: channel.send(message))

    def _project(self, run: RunRecord, status: str) -> None:
        assert self.status_writer is not None and run.project_item_id is not None
        self.status_writer.set_status(run.project_item_id, status)
        self.store.checkpoint(run.id, summary="Project reflete intervenção humana", project_status=status)

    def _attempt(self, run: RunRecord, key: str, channel: str, send: Callable[[], None]) -> None:
        policy = self.config.notifications
        if not self.store.claim_notification(run.id, key, channel,
                                             max_attempts=policy.max_attempts,
                                             retry_seconds=policy.retry_seconds):
            return
        try:
            send()
        except Exception:
            # Exceções de SMTP/HTTP podem conter credenciais e nunca são serializadas.
            sent = False
        else:
            sent = True
        self.store.finish_notification(run.id, key, channel, sent=sent)
