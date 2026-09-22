"""Proteções globais para impedir efeitos externos na suíte de testes."""

import os
from pathlib import Path
import socket
import subprocess

import pytest

from ai_dev_orchestrator.infrastructure import process


@pytest.fixture(autouse=True)
def isolate_notification_environment(monkeypatch: pytest.MonkeyPatch, request) -> None:
    """Credenciais herdadas e conexões de rede nunca alcançam providers reais."""
    for name in tuple(os.environ):
        if name == "ORCH_NOTIFICATIONS" or name.startswith(
            ("ORCH_SMTP_", "ORCH_EMAIL_", "ORCH_DISCORD_", "ORCH_TELEGRAM_")
        ):
            monkeypatch.delenv(name, raising=False)

    def reject_network(*_args, **_kwargs):
        raise AssertionError("A suíte de testes não pode abrir conexões de rede")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket.socket, "sendto", reject_network)

    # Só o probe opt-in pode alcançar CLIs externas; testes comuns devem injetar doubles.
    live_probe = (
        request.node.path.name == "test_antigravity_live.py"
        and os.environ.get("ORCH_TEST_ANTIGRAVITY_LIVE") == "1"
    )
    original_init = subprocess.Popen.__init__

    def safe_init(self, args, *positional, **kwargs):
        if not live_probe:
            executable = args[0] if isinstance(args, (tuple, list)) else args.split()[0]
            name = Path(executable).stem.casefold()
            if name in {"codex", "agy", "gemini", "gh", "curl", "wget", "uvx", "code-review-graph"}:
                raise AssertionError("CLI externa real proibida na suíte local")
        original_init(self, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", safe_init)
    original_capture = process.run_captured

    def safe_capture(command, **kwargs):
        name = Path(command[0]).stem.casefold()
        if not live_probe and name in {"codex", "agy", "gemini", "gh", "curl", "wget", "uvx", "code-review-graph"}:
            raise AssertionError("CLI externa real proibida na suíte local")
        return original_capture(command, **kwargs)

    # Windows inicia um bootstrap Python antes do comando: proteger antes dessa fronteira.
    monkeypatch.setattr(process, "run_captured", safe_capture)
