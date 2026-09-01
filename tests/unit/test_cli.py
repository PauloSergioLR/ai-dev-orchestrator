"""Testes da CLI."""

from typer.testing import CliRunner

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.cli import app

runner = CliRunner()


def test_help_is_available() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Orquestrador local-first de desenvolvimento com IA." in result.output


def test_version_is_available() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"{__version__}\n"
