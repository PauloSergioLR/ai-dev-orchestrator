"""Canais operacionais; credenciais são lidas do ambiente e não persistidas."""

import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REQUIRED_ENV = {
    "email": ("ORCH_SMTP_HOST", "ORCH_SMTP_USER", "ORCH_SMTP_PASSWORD", "ORCH_EMAIL_FROM", "ORCH_EMAIL_TO"),
    "discord": ("ORCH_DISCORD_WEBHOOK",),
    "telegram": ("ORCH_TELEGRAM_TOKEN", "ORCH_TELEGRAM_CHAT_ID"),
}


def missing_environment(channels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for channel in channels for name in REQUIRED_ENV[channel] if not os.environ.get(name))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirecionamento de notificação recusado")


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
            url = env["ORCH_DISCORD_WEBHOOK"]
            if not url.startswith("https://discord.com/api/webhooks/"):
                raise ValueError("Endpoint Discord inválido")
            parts = urlsplit(url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, "wait=true", ""))
            payload = {"content": message, "allowed_mentions": {"parse": []}}
        elif self.channel == "telegram":
            url = "https://api.telegram.org/bot" + env["ORCH_TELEGRAM_TOKEN"] + "/sendMessage"
            payload = {"chat_id": env["ORCH_TELEGRAM_CHAT_ID"], "text": message}
        else:
            raise ValueError("Canal desconhecido")
        request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
            if self.channel == "telegram" and json.loads(response.read(65536)).get("ok") is not True:
                raise ValueError("Entrega recusada")
