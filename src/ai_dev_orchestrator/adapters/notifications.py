"""Canais operacionais; credenciais são lidas do ambiente e não persistidas."""

import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REQUIRED_ENV = {
    "email": ("ORCH_SMTP_HOST", "ORCH_SMTP_USER", "ORCH_SMTP_PASSWORD", "ORCH_EMAIL_FROM", "ORCH_EMAIL_TO"),
    # Os nomes curtos são mantidos para instalações anteriores.
    "discord": ("ORCH_DISCORD_WEBHOOK",),
    "telegram": ("ORCH_TELEGRAM_TOKEN", "ORCH_TELEGRAM_CHAT_ID"),
}


def missing_environment(channels: tuple[str, ...]) -> tuple[str, ...]:
    missing = []
    for channel in channels:
        if channel == "discord":
            if not (os.environ.get("ORCH_DISCORD_WEBHOOK_URL") or os.environ.get("ORCH_DISCORD_WEBHOOK")):
                missing.append("ORCH_DISCORD_WEBHOOK_URL")
        elif channel == "telegram":
            if not (os.environ.get("ORCH_TELEGRAM_BOT_TOKEN") or os.environ.get("ORCH_TELEGRAM_TOKEN")):
                missing.append("ORCH_TELEGRAM_BOT_TOKEN")
            if not os.environ.get("ORCH_TELEGRAM_CHAT_ID"):
                missing.append("ORCH_TELEGRAM_CHAT_ID")
        else:
            missing.extend(name for name in REQUIRED_ENV[channel] if not os.environ.get(name))
    return tuple(missing)


def configuration_error(channel: str) -> str | None:
    """Retorna somente um diagnóstico seguro de formato, nunca o valor secreto."""
    if channel == "discord":
        url = os.environ.get("ORCH_DISCORD_WEBHOOK_URL") or os.environ.get("ORCH_DISCORD_WEBHOOK")
        if url and not url.startswith("https://discord.com/api/webhooks/"):
            return "URL do webhook Discord deve usar https://discord.com/api/webhooks/"
    if channel == "telegram" and os.environ.get("ORCH_TELEGRAM_CHAT_ID", "").strip() == "":
        return "chat_id do Telegram não pode ser vazio"
    return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirecionamento de notificação recusado")


class NotificationProvider:
    """Porta comum para providers externos de notificação."""

    channel: str

    def send(self, message: str) -> None:
        raise NotImplementedError


class _HttpNotificationProvider(NotificationProvider):
    def __init__(self, timeout: float = 15) -> None:
        self.timeout = timeout

    def _post(self, url: str, payload: dict[str, object]) -> bytes:
        request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.read(65536)
        except HTTPError as error:
            if error.code != 429:
                raise ValueError(f"Provider recusou a entrega (HTTP {error.code})") from None
            # Discord e Telegram devolvem Retry-After; tenta uma vez, sempre limitada.
            retry_after = min(float(error.headers.get("Retry-After", "1")), self.timeout)
            time.sleep(max(0, retry_after))
            with opener.open(request, timeout=self.timeout) as response:
                return response.read(65536)


class DiscordWebhookProvider(_HttpNotificationProvider):
    channel = "discord"

    def send(self, message: str) -> None:
        url = os.environ.get("ORCH_DISCORD_WEBHOOK_URL") or os.environ.get("ORCH_DISCORD_WEBHOOK")
        if not url:
            raise ValueError("Configuração de ambiente incompleta")
        if not url.startswith("https://discord.com/api/webhooks/"):
            raise ValueError("Endpoint Discord inválido")
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "wait=true", ""))
        self._post(url, {"content": message, "allowed_mentions": {"parse": []}})


class TelegramBotProvider(_HttpNotificationProvider):
    channel = "telegram"

    def send(self, message: str) -> None:
        token = os.environ.get("ORCH_TELEGRAM_BOT_TOKEN") or os.environ.get("ORCH_TELEGRAM_TOKEN")
        chat_id = os.environ.get("ORCH_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ValueError("Configuração de ambiente incompleta")
        response = self._post("https://api.telegram.org/bot" + token + "/sendMessage", {"chat_id": chat_id, "text": message})
        try:
            accepted = json.loads(response).get("ok") is True
        except json.JSONDecodeError:
            accepted = False
        if not accepted:
            raise ValueError("Entrega Telegram recusada; confira token e chat_id")


class EnvironmentNotificationAdapter:
    def __init__(self, channel: str, timeout: float = 15) -> None:
        self.channel, self.timeout = channel, timeout

    def send(self, message: str) -> None:
        if missing_environment((self.channel,)):
            raise ValueError("Configuração de ambiente incompleta")
        env = os.environ
        if self.channel == "email":
            mail = EmailMessage()
            mail["Subject"] = "AI Dev Orchestrator: intervenção humana necessária"
            mail["From"], mail["To"] = env["ORCH_EMAIL_FROM"], env["ORCH_EMAIL_TO"]
            mail.set_content(message)
            with smtplib.SMTP(env["ORCH_SMTP_HOST"], int(env.get("ORCH_SMTP_PORT", "587")), timeout=self.timeout) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(env["ORCH_SMTP_USER"], env["ORCH_SMTP_PASSWORD"])
                smtp.send_message(mail)
            return
        if self.channel == "discord":
            return DiscordWebhookProvider(self.timeout).send(message)
        elif self.channel == "telegram":
            return TelegramBotProvider(self.timeout).send(message)
        else:
            raise ValueError("Canal desconhecido")
