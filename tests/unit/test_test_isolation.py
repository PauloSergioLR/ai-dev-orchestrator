"""A suíte prova suas barreiras sem alcançar a rede ou um provider."""

import socket
import subprocess

import pytest


@pytest.mark.parametrize("method", ["connect", "connect_ex", "sendto"])
def test_socket_direto_e_bloqueado(method):
    with socket.socket() as connection, pytest.raises(AssertionError, match="rede"):
        getattr(connection, method)(("example.invalid", 443))


@pytest.mark.parametrize("executable", ["codex", "agy.exe", "gh", "uvx"])
def test_provider_real_e_bloqueado_antes_de_iniciar(executable):
    with pytest.raises(AssertionError, match="CLI externa"):
        subprocess.run([executable, "--version"], check=True)


def test_provider_bloqueado_antes_do_bootstrap_windows():
    from ai_dev_orchestrator.infrastructure import process

    with pytest.raises(AssertionError, match="CLI externa"):
        process.run_captured(["codex", "--version"], timeout=1)
