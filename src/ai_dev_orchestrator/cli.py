"""Interface de linha de comando do AI Dev Orchestrator."""

import typer

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.services.doctor import DoctorService, has_errors

app = typer.Typer(
    help="Orquestrador local-first de desenvolvimento com IA.",
    add_completion=False,
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def cli(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="Exibe a versão da aplicação e encerra.",
    ),
) -> None:
    """Interface de linha de comando do AI Dev Orchestrator."""


@app.command()
def doctor() -> None:
    """Diagnostica os pré-requisitos locais sem corrigir problemas."""
    checks = DoctorService().diagnose()

    typer.echo("AI Dev Orchestrator Doctor\n")
    for check in checks:
        typer.echo(f"{check.name:<18} {check.status.value:<7} {check.message}")

    if has_errors(checks):
        raise typer.Exit(code=1)


def main() -> None:
    """Executa a aplicação de linha de comando."""
    app()
