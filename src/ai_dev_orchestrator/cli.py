"""Interface de linha de comando do AI Dev Orchestrator."""

import typer

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.config import ConfigurationError, load_config
from ai_dev_orchestrator.services.doctor import DoctorService, has_errors
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunPipelineError, RunResult

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


@app.command()
def run(
    issue: int = typer.Option(..., "--issue", min=1, help="Número positivo da Issue."),
    branch: str = typer.Option(..., "--branch", help="Nome da nova branch."),
) -> None:
    """Prepara uma Issue elegível e inicia sua sessão Codex."""
    if not branch.strip():
        raise typer.BadParameter("--branch é obrigatória e não pode ser vazia", param_hint="--branch")
    try:
        result = RunPipeline.from_config(load_config()).run(issue, branch)
    except (ConfigurationError, RunPipelineError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    _show_run_result(result)


def _show_run_result(result: RunResult) -> None:
    """Exibe o resumo humano sem expor o JSONL do provider."""
    typer.echo(f"Issue: #{result.issue_number}")
    typer.echo(f"Item do Project: {result.project_item_id}")
    typer.echo(f"Branch: {result.branch}")
    typer.echo(f"Worktree: {result.worktree_path}")
    typer.echo(f"Base: {result.base_ref}")
    typer.echo(f"Status: {result.project_status}")
    typer.echo(f"Sessão Codex: {result.session_id}")
    typer.echo(f"Mensagem final: {result.final_message}")
    typer.echo(f"Gates locais: {', '.join(gate.name for gate in result.gates)}")
    typer.echo(f"Commit: {result.commit_sha}")
    typer.echo(f"Remote: {result.remote_name}")
    typer.echo(f"Pull Request: #{result.pull_request_number} {result.pull_request_url}")
    typer.echo(f"Base do Pull Request: {result.pull_request_base}")


def main() -> None:
    """Executa a aplicação de linha de comando."""
    app()
