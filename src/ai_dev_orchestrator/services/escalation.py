"""Persistência, Project e notificações de uma intervenção humana."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Protocol

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.notification import HumanRequiredNotification, NotificationChannel
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


class ProjectStatusWriter(Protocol):
    def set_status(self, project_item_id: str, status_name: str) -> None: ...


class HumanEscalationService:
    def __init__(
        self,
        config: OrchestratorConfig,
        store: SqliteExecutionStore,
        status_writer: ProjectStatusWriter | None = None,
        channels: tuple[NotificationChannel, ...] = (),
    ) -> None:
        self.config, self.store = config, store
        self.status_writer, self.channels = status_writer, channels

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "HumanEscalationService":
        from ai_dev_orchestrator.adapters.github import GitHubProjectStatusAdapter
        from ai_dev_orchestrator.adapters.notifications import configured_channels

        return cls(
            config,
            SqliteExecutionStore(config.state.database_path),
            GitHubProjectStatusAdapter(config),
            configured_channels(config.notifications),
        )

    def escalate(
        self,
        execution_id: str,
        reason_code: str,
        reason: str,
        *,
        classification: str | None = None,
        suggested_action: str | None = None,
    ) -> RunRecord:
        run = self.store.get(execution_id)
        occurred_at = datetime.now(timezone.utc)
        classification = classification or run.failure_classification
        suggested_action = suggested_action or run.suggested_action
        reason = self._without_configured_secrets(reason)
        if suggested_action:
            suggested_action = self._without_configured_secrets(suggested_action)
        if run.phase != ExecutionPhase.HUMAN_REQUIRED:
            blocked_phase = run.phase.value
            run = self.store.transition(
                execution_id,
                ExecutionPhase.HUMAN_REQUIRED,
                summary=f"Intervenção humana necessária: {reason_code}",
                human_reason_code=reason_code,
                human_reason=reason,
                blocked_phase=blocked_phase,
                failure_classification=classification,
                suggested_action=suggested_action,
                human_required_at=occurred_at.isoformat(),
                last_error=reason,
            )
        elif (
            run.human_reason_code != reason_code
            or run.human_reason != reason
            or run.failure_classification != classification
            or run.suggested_action != suggested_action
        ):
            # Permite uma nova causa material sem criar outra execução.
            run = self.store.checkpoint(
                execution_id,
                summary=f"Causa de intervenção atualizada: {reason_code}",
                human_reason_code=reason_code,
                human_reason=reason,
                failure_classification=classification,
                suggested_action=suggested_action,
                human_required_at=occurred_at.isoformat(),
                last_error=reason,
            )
        if self.status_writer and run.project_item_id and run.project_status != self.config.github.human_required_status:
            try:
                self.status_writer.set_status(run.project_item_id, self.config.github.human_required_status)
                run = self.store.checkpoint(
                    execution_id,
                    summary="Project marcado para intervenção humana",
                    project_status=self.config.github.human_required_status,
                )
            except Exception:
                # Estado local e os demais canais não dependem da disponibilidade do Project.
                pass
        self._dispatch(self._event(run))
        return self.store.get(execution_id)

    def retry_failed_notifications(self, execution_id: str) -> None:
        """Retenta apenas entregas falhas, sem repetir qualquer mutação do pipeline."""
        run = self.store.get(execution_id)
        if run.phase != ExecutionPhase.HUMAN_REQUIRED:
            return
        self._dispatch(self._event(run), retry_failed=True)

    def _dispatch(
        self, event: HumanRequiredNotification, *, retry_failed: bool = False
    ) -> None:
        for channel in self.channels:
            if not self.store.begin_notification(
                event.execution_id,
                event.event_key,
                channel.name,
                retry_failed=retry_failed,
            ):
                continue
            try:
                channel.send(event)
            except Exception as error:
                self.store.finish_notification(
                    event.execution_id, event.event_key, channel.name, str(error)
                )
            else:
                self.store.finish_notification(
                    event.execution_id, event.event_key, channel.name
                )

    def _without_configured_secrets(self, value: str) -> str:
        env_names = (
            self.config.notifications.smtp_username_env,
            self.config.notifications.smtp_password_env,
            self.config.notifications.discord_webhook_env,
            self.config.notifications.telegram_token_env,
            self.config.notifications.telegram_chat_id_env,
        )
        for name in env_names:
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, "[redigido]")
        return value

    def _event(self, run: RunRecord) -> HumanRequiredNotification:
        assert run.human_reason_code and run.human_reason and run.blocked_phase
        return HumanRequiredNotification(
            run.id,
            self.config.github.repository_full_name,
            run.issue_number,
            run.human_reason_code,
            run.human_reason,
            run.blocked_phase,
            run.human_required_at or datetime.now(timezone.utc),
            run.branch,
            run.worktree_path,
            run.codex_session_id,
            run.pull_request_number,
            run.pull_request_url,
            run.current_head_sha,
            run.ci_head_sha,
            run.review_verdict,
            run.correction_attempts,
            run.failure_classification,
            run.suggested_action,
        )
