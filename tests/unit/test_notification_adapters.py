"""Verifica os contratos dos canais sem SMTP ou HTTP reais."""

import json
from unittest.mock import MagicMock

import pytest

from ai_dev_orchestrator.adapters import notifications
from ai_dev_orchestrator.adapters.notifications import (
    DiscordWebhookProvider,
    EnvironmentNotificationAdapter,
    REQUIRED_ENV,
    TelegramBotProvider,
)


def configure(monkeypatch, channel):
    for name in REQUIRED_ENV[channel]:
        monkeypatch.setenv(name, "valor-local")


def test_email_exige_tls_antes_da_autenticacao(monkeypatch):
    configure(monkeypatch, "email")
    smtp_factory = MagicMock()
    monkeypatch.setattr(notifications.smtplib, "SMTP", smtp_factory)
    EnvironmentNotificationAdapter("email").send("Issue #45 HUMAN_REQUIRED")
    smtp = smtp_factory.return_value.__enter__.return_value
    assert [call[0] for call in smtp.method_calls] == ["starttls", "login", "send_message"]
    assert "HUMAN_REQUIRED" in smtp.send_message.call_args.args[0].get_content()
    assert "ORCH_SMTP_PASSWORD" not in str(smtp.send_message.call_args.args[0])


@pytest.mark.parametrize("channel", ["discord", "telegram"])
def test_http_envia_payload_operacional_sem_segredo_no_corpo(monkeypatch, channel):
    configure(monkeypatch, channel)
    monkeypatch.setenv("ORCH_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/id/segredo")
    monkeypatch.setenv("ORCH_TELEGRAM_TOKEN", "token-secreto")
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
    monkeypatch.setattr(notifications, "build_opener", lambda *args: opener)
    EnvironmentNotificationAdapter(channel, timeout=7).send("Issue #45 HUMAN_REQUIRED")
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert opener.open.call_args.kwargs["timeout"] == 7
    assert "segredo" not in request.data.decode() and "token-secreto" not in request.data.decode()
    if channel == "discord":
        assert request.full_url.endswith("?wait=true")
        assert payload["allowed_mentions"] == {"parse": []}
    else:
        assert payload["text"] == "Issue #45 HUMAN_REQUIRED"
        assert "parse_mode" not in payload


def test_telegram_recusa_resposta_sem_confirmacao(monkeypatch):
    configure(monkeypatch, "telegram")
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":false}'
    monkeypatch.setattr(notifications, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError, match="recusada"):
        EnvironmentNotificationAdapter("telegram").send("mensagem")


def test_ambiente_incompleto_nao_abre_conexao(monkeypatch):
    monkeypatch.delenv("ORCH_DISCORD_WEBHOOK", raising=False)
    opener = MagicMock()
    monkeypatch.setattr(notifications, "build_opener", opener)
    with pytest.raises(ValueError, match="incompleta"):
        EnvironmentNotificationAdapter("discord").send("mensagem")
    opener.assert_not_called()


def test_redirecionamentos_nao_recebem_payload():
    with pytest.raises(ValueError, match="Redirecionamento"):
        notifications._NoRedirect().redirect_request(None, None, 307, "", {}, "https://outro.invalid")


@pytest.mark.parametrize(
    ("provider", "environment"),
    [
        (DiscordWebhookProvider, {"ORCH_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/id/novo-segredo"}),
        (TelegramBotProvider, {"ORCH_TELEGRAM_BOT_TOKEN": "novo-token", "ORCH_TELEGRAM_CHAT_ID": "-100123"}),
    ],
)
def test_providers_aceitam_nomes_de_secrets_documentados(monkeypatch, provider, environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
    monkeypatch.setattr(notifications, "build_opener", lambda *args: opener)
    provider(timeout=4).send("teste seguro")
    assert opener.open.call_args.kwargs["timeout"] == 4
    payload = opener.open.call_args.args[0].data.decode()
    assert "novo-segredo" not in payload and "novo-token" not in payload


def test_init_configura_canais_e_mostra_apenas_nomes_ausentes(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ai_dev_orchestrator.cli import app
    from ai_dev_orchestrator.config import load_config
    from ai_dev_orchestrator.services.init_project import ProjectDiscovery, ProjectInitService
    discovery = ProjectDiscovery(tmp_path, "origin", "https://github.com/acme/repo.git", "acme", "repo", "main", ("main",), "main", (), (1,), ("origin",))
    monkeypatch.setattr(ProjectInitService, "discover", lambda self, cwd: discovery)
    for name in REQUIRED_ENV["email"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_SMTP_PASSWORD", "senha-privada-do-teste")
    result = CliRunner().invoke(app, ["init", "--notifications"], input="\n\n\n\n\nY\nemail,discord\nY\n")
    assert result.exit_code == 0, result.output
    assert "ORCH_SMTP_HOST" in result.output
    assert "senha-privada-do-teste" not in result.output
    content = (tmp_path / "orchestrator.toml").read_text(encoding="utf-8")
    assert "senha-privada-do-teste" not in content
    assert load_config(tmp_path / "orchestrator.toml").notifications.channels == ("email", "discord")
