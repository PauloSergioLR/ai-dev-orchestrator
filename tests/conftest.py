"""Proteções globais para impedir efeitos externos na suíte de testes."""

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def isolate_notification_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credenciais herdadas e conexões de rede nunca alcançam providers reais."""
    for name in tuple(os.environ):
        if name == "ORCH_NOTIFICATIONS" or name.startswith(
            ("ORCH_SMTP_", "ORCH_EMAIL_", "ORCH_DISCORD_", "ORCH_TELEGRAM_")
        ):
            monkeypatch.delenv(name, raising=False)

    def reject_network(*_args, **_kwargs):
        raise AssertionError("A suíte de testes não pode abrir conexões de rede")

    monkeypatch.setattr(socket, "create_connection", reject_network)
