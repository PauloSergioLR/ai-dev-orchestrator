"""Interface de linha de comando do AI Dev Orchestrator."""

import typer

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.config import ConfigurationError, load_config
from ai_dev_orchestrator.services.doctor import DoctorService, has_errors
from ai_dev_orchestrator.services.pipeline import (
    RunPipeline,
    RunPipelineError,
    RunResult,
)
from ai_dev_orchestrator.infrastructure.database import (
    ExecutionStoreError,
    SqliteExecutionStore,
)
from ai_dev_orchestrator.services.resume import ResumeError, ResumeService

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
        raise typer.BadParameter(
            "--branch é obrigatória e não pode ser vazia", param_hint="--branch"
        )
    try:
        result = RunPipeline.from_config(load_config()).run(issue, branch)
    except (ConfigurationError, RunPipelineError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    _show_run_result(result)


@app.command()
def state(
    issue: int = typer.Option(..., "--issue", min=1, help="Número positivo da Issue."),
) -> None:
    """Exibe, sem modificar, o último estado ativo persistido de uma Issue."""
    try:
        record = SqliteExecutionStore(
            load_config().state.database_path
        ).get_latest_for_issue(issue)
    except (ConfigurationError, ExecutionStoreError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    if record is None:
        typer.echo(f"Nenhuma execução encontrada para a Issue #{issue}.")
        raise typer.Exit(code=1)
    typer.echo(f"Issue: #{record.issue_number}")
    typer.echo(f"Phase: {record.phase}")
    typer.echo(f"Branch: {record.branch or '-'}")
    typer.echo(f"Sessão Codex: {record.codex_session_id or '-'}")
    typer.echo(f"PR: #{record.pull_request_number or '-'}")
    typer.echo(f"HEAD: {record.current_head_sha or '-'}")
    typer.echo(f"Correções: {record.correction_attempts}")
    typer.echo(f"Atualizado em: {record.updated_at.isoformat()}")


@app.command()
def resume(
    issue: int = typer.Option(..., "--issue", min=1, help="Número positivo da Issue."),
) -> None:
    """Retoma uma execução ativa a partir do estado persistido."""
    try:
        result = ResumeService.from_config(load_config()).resume(issue)
    except (ConfigurationError, ResumeError, ExecutionStoreError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Issue: #{result.issue_number}")
    typer.echo(f"Execução: {result.execution_id}")
    typer.echo(f"Fase: {result.phase}")
    typer.echo(f"Branch: {result.branch or '-'}")
    typer.echo(f"Sessão Codex: {result.codex_session_id or '-'}")
    typer.echo(f"PR: #{result.pull_request_number or '-'}")
    typer.echo(f"HEAD: {result.current_head_sha or '-'}")


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
    if result.ci_status is not None:
        typer.echo(f"HEAD validado do Pull Request: {result.pull_request_head_sha}")
        typer.echo(f"CI: {result.ci_status}")
        typer.echo(
            "Checks obrigatórios observados: "
            + ", ".join(
                f"{check.name} ({check.status}/{check.conclusion or 'sem conclusão'})"
                for check in result.ci_checks
            )
        )
    if result.review is not None:
        typer.echo(f"Review Gemini: {result.review.verdict}")
        typer.echo(
            f"Tentativas de review/correção: {result.review_attempts}/{result.correction_attempts}"
        )
        typer.echo(f"HEAD final revisado: {result.final_reviewed_head_sha}")
        typer.echo(f"Findings anteriores preservados: {result.prior_findings_count}")
        blocking = [
            finding
            for finding in result.review.findings
            if finding.severity.value in result.blocking_severities
        ]
        typer.echo(f"Findings bloqueantes: {len(blocking)}")
        for finding in blocking:
            typer.echo(f"- {finding.severity}: {finding.title}")
    if result.auto_merge_enabled:
        typer.echo(f"Auto-merge: {result.merge_status}")
        if result.merged:
            typer.echo(f"Merge commit: {result.merge_commit_sha}")


def main() -> None:
    """Executa a aplicação de linha de comando."""
    app()
