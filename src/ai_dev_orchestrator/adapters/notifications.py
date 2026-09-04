"""Adapters de notificação; segredos são lidos somente do ambiente no envio."""

from __future__ import annotations

from email.message import EmailMessage
import json
import os
import smtplib
from urllib import parse, request

from ai_dev_orchestrator.config import NotificationsConfig
from ai_dev_orchestrator.domain.notification import HumanRequiredNotification, NotificationChannel


class NotificationDeliveryError(Exception):
    pass


class EmailNotificationChannel:
    name = "email"

    def __init__(self, config: NotificationsConfig) -> None:
        self.config = config

    def send(self, event: HumanRequiredNotification) -> None:
        username = os.environ.get(self.config.smtp_username_env)
        password = os.environ.get(self.config.smtp_password_env)
        if not self.config.smtp_sender or not self.config.email_recipients:
            raise NotificationDeliveryError("Remetente ou destinatário de e-mail não configurado")
        message = EmailMessage()
        message["Subject"] = f"[HUMAN_REQUIRED] {event.repository} Issue #{event.issue_number}"
        message["From"] = self.config.smtp_sender
        message["To"] = ", ".join(self.config.email_recipients)
        message.set_content(event.message())
        try:
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=15) as smtp:
                if self.config.smtp_starttls:
                    smtp.starttls()
                if username or password:
                    if not username or not password:
                        raise NotificationDeliveryError("Credenciais SMTP incompletas")
                    smtp.login(username, password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise NotificationDeliveryError(f"Falha ao enviar e-mail: {type(error).__name__}") from error


class DiscordNotificationChannel:
    name = "discord"

    def __init__(self, webhook_env: str) -> None:
        self.webhook_env = webhook_env

    def send(self, event: HumanRequiredNotification) -> None:
        url = os.environ.get(self.webhook_env)
        if not url:
            raise NotificationDeliveryError(f"Variável {self.webhook_env} ausente")
        _post_json(url, {"content": event.message()})


class TelegramNotificationChannel:
    name = "telegram"

    def __init__(self, token_env: str, chat_id_env: str) -> None:
        self.token_env, self.chat_id_env = token_env, chat_id_env

    def send(self, event: HumanRequiredNotification) -> None:
        token, chat_id = os.environ.get(self.token_env), os.environ.get(self.chat_id_env)
        if not token or not chat_id:
            raise NotificationDeliveryError(
                f"Variáveis {self.token_env} e/ou {self.chat_id_env} ausentes"
            )
        url = f"https://api.telegram.org/bot{parse.quote(token, safe=':')}/sendMessage"
        _post_json(url, {"chat_id": chat_id, "text": event.message()})


def configured_channels(config: NotificationsConfig) -> tuple[NotificationChannel, ...]:
    factories = {
        "email": lambda: EmailNotificationChannel(config),
        "discord": lambda: DiscordNotificationChannel(config.discord_webhook_env),
        "telegram": lambda: TelegramNotificationChannel(
            config.telegram_token_env, config.telegram_chat_id_env
        ),
    }
    return tuple(factories[name]() for name in config.channels)


def _post_json(url: str, payload: dict[str, str]) -> None:
    data = json.dumps(payload).encode("utf-8")
    outbound = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(outbound, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise NotificationDeliveryError(f"Canal HTTP retornou status {response.status}")
    except OSError as error:
        # A URL (que pode conter segredo) nunca entra na mensagem persistida.
        raise NotificationDeliveryError(f"Falha no canal HTTP: {type(error).__name__}") from error
