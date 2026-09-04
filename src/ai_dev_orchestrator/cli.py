"""Interface de linha de comando do AI Dev Orchestrator."""

import os
import typer
from pathlib import Path

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
from ai_dev_orchestrator.services.work import WorkError, WorkService
from ai_dev_orchestrator.services.init_project import ProjectInitError, ProjectInitService
from ai_dev_orchestrator.services.supervisor import SupervisorError, SupervisorService
from ai_dev_orchestrator.config import OrchestratorConfig

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


@app.command("init")
def init_project(
    advanced: bool = typer.Option(
        False, "--advanced", help="Permite ajustar polling e timeouts."
    ),
) -> None:
    """Descobre e grava interativamente o perfil local deste projeto."""
    service = ProjectInitService()
    try:
        found = service.discover(Path.cwd())
    except ProjectInitError as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    path = found.repository_path / "orchestrator.toml"
    existing = None
    if path.exists():
        try:
            existing = load_config(path)
        except ConfigurationError as error:
            typer.echo(f"Erro: {error}", err=True)
            raise typer.Exit(code=1) from error
        if existing.workspace.repository_path.resolve() != found.repository_path:
            raise typer.BadParameter(
                "repository_path configurado diverge do repositório Git detectado"
            )
    typer.echo("AI Dev Orchestrator — Configuração do projeto\n")
    detected_repo = (
        f"{found.owner}/{found.repository}" if found.owner and found.repository else "não identificado"
    )
    typer.echo(f"Repositório detectado: {detected_repo}")
    typer.echo(f"Default branch: {found.default_branch or 'não detectada'}")
    typer.echo(f"Branches relevantes: {', '.join(found.branches) or 'nenhuma detectada'}")
    for evidence in found.evidence:
        typer.echo(f"Interpretação: {evidence}")
    remote_name = (
        existing.workspace.remote_name
        if existing and existing.workspace.remote_name in found.remote_names
        else found.remote_name
    )
    if len(found.remote_names) > 1 and "origin" not in found.remote_names:
        remote_name = _prompt_branch("Remote Git", found.remote_names, None)
    owner = existing.github.owner if existing else found.owner
    repository = existing.github.repository if existing else found.repository
    if not owner or not repository:
        owner = typer.prompt("Owner do GitHub")
        repository = typer.prompt("Repositório do GitHub")
    elif found.owner and existing and (
        owner != found.owner or repository != found.repository
    ):
        raise typer.BadParameter(
            "Configuração existente diverge do remote GitHub detectado"
        )
    choices = found.branches or tuple(
        value for value in (found.suggested_base_branch, found.default_branch) if value
    )
    base_default = existing.workspace.base_branch if existing else (
        found.suggested_base_branch or (choices[0] if len(choices) == 1 else None)
    )
    base = _prompt_branch("Base das novas branches", choices, base_default)
    target_default = existing.github.pull_request_target if existing else base
    target = _prompt_branch("Destino dos Pull Requests", choices, target_default)
    protected_default = existing.github.protected_branches if existing else (
        ("main",) if "main" in choices else ()
    )
    protected_text = typer.prompt(
        "Branches protegidas (separadas por vírgula; vazio = nenhuma)",
        default=", ".join(protected_default), show_default=True,
    )
    protected = tuple(value.strip() for value in protected_text.split(",") if value.strip())
    project_number = existing.github.project_number if existing else (
        found.github_projects[0]
        if len(found.github_projects) == 1
        else typer.prompt("Número do GitHub Project", type=int)
    )
    codex_model = typer.prompt(
        "Modelo Codex (default/auto ou identificador explícito)",
        default=existing.providers.codex_model if existing else "default",
    )
    if found.gemini_models:
        typer.echo("Modelos enumerados pelo Antigravity: " + ", ".join(found.gemini_models))
    gemini_model = typer.prompt(
        "Modelo Gemini (default/auto ou identificador explícito)",
        default=existing.providers.gemini_model if existing else "default",
    )
    normalized_gemini = "default" if gemini_model.casefold() in {"default", "auto"} else gemini_model
    if found.gemini_models and normalized_gemini != "default" and normalized_gemini not in found.gemini_models:
        raise typer.BadParameter("Modelo Gemini não consta na enumeração da CLI")
    values = {
        "github": {
            "owner": owner, "repository": repository, "project_number": project_number,
            "ready_status": existing.github.ready_status if existing else "Ready",
            "in_progress_status": existing.github.in_progress_status if existing else "In Progress",
            "ai_review_status": existing.github.ai_review_status if existing else "AI Review",
            "done_status": existing.github.done_status if existing else "Done",
            "human_required_status": existing.github.human_required_status if existing else "Human Review",
            "pull_request_target": target, "protected_branches": protected,
            "status_field_name": existing.github.status_field_name if existing else "Status",
        },
        "workspace": {
            "repository_path": found.repository_path,
            "worktrees_dir": existing.workspace.worktrees_dir if existing else found.repository_path.parent / f"{found.repository_path.name}-worktrees",
            "base_branch": base, "remote_name": remote_name,
        },
        "providers": {"codex_model": codex_model, "gemini_model": gemini_model},
        "execution": existing.execution.model_dump() if existing else {"max_attempts": 2, "max_parallel_runs": 1, "auto_merge": False},
        "state": existing.state.model_dump() if existing else {},
        "ci": existing.ci.model_dump() if existing else {},
        "convergence": existing.convergence.model_dump() if existing else {},
        "review": existing.review.model_dump() if existing else {},
        "supervisor": existing.supervisor.model_dump() if existing else {},
        "notifications": existing.notifications.model_dump() if existing else {},
    }
    if advanced:
        values["ci"]["poll_interval_seconds"] = typer.prompt(
            "Polling da CI (segundos)",
            default=values["ci"].get("poll_interval_seconds", 5), type=float,
        )
        values["ci"]["timeout_seconds"] = typer.prompt(
            "Timeout da CI (segundos)",
            default=values["ci"].get("timeout_seconds", 900), type=float,
        )
        values["supervisor"]["poll_interval_seconds"] = typer.prompt(
            "Polling do supervisor (segundos)",
            default=values["supervisor"].get("poll_interval_seconds", 60), type=float,
        )
        values["supervisor"]["max_sleep_seconds"] = typer.prompt(
            "Espera máxima por ciclo (segundos)",
            default=values["supervisor"].get("max_sleep_seconds", 300), type=float,
        )
    typer.echo(
        f"\nResumo: base={base}; target={target}; protegidas="
        f"{', '.join(protected) or 'nenhuma'}; Project={project_number}; "
        f"auto-merge={values['execution']['auto_merge']}; "
        f"correções={values['review'].get('max_correction_attempts', 3)}; "
        f"checks={', '.join(values['ci'].get('required_checks', ('test',)))}; "
        f"worktrees={values['workspace']['worktrees_dir']}"
    )
    typer.confirm("Salvar configuração?", default=True, abort=True)
    if existing is None and typer.confirm(
        "Deseja configurar notificações operacionais?", default=False
    ):
        channel_text = typer.prompt(
            "Canais (email, discord, telegram; separados por vírgula)"
        )
        channels = tuple(
            value.strip().casefold()
            for value in channel_text.split(",")
            if value.strip()
        )
        values["notifications"]["channels"] = channels
        if "email" in channels:
            values["notifications"]["smtp_host"] = typer.prompt("Host SMTP")
            values["notifications"]["smtp_sender"] = typer.prompt("Remetente SMTP")
            recipients = typer.prompt("Destinatários de e-mail (separados por vírgula)")
            values["notifications"]["email_recipients"] = tuple(
                value.strip() for value in recipients.split(",") if value.strip()
            )
        required = {
            "email": ("ORCH_SMTP_USERNAME", "ORCH_SMTP_PASSWORD"),
            "discord": ("ORCH_DISCORD_WEBHOOK_URL",),
            "telegram": ("ORCH_TELEGRAM_BOT_TOKEN", "ORCH_TELEGRAM_CHAT_ID"),
        }
        for channel in channels:
            if channel in required:
                missing = tuple(name for name in required[channel] if not os.environ.get(name))
                if missing:
                    typer.echo(
                        f"Variáveis de ambiente ausentes para {channel}: {', '.join(missing)}"
                    )
    try:
        config = OrchestratorConfig(**values)
        service.write(path, config)
    except (ValueError, ProjectInitError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Configuração salva em {path}")


def _prompt_branch(label: str, choices: tuple[str, ...], default: str | None) -> str:
    if default and (len(choices) <= 1 or default in choices):
        return typer.prompt(label, default=default)
    if not choices:
        return typer.prompt(label)
    typer.echo(f"\n{label}:")
    for index, branch in enumerate(choices, 1):
        typer.echo(f"[{index}] {branch}")
    selected = typer.prompt("Escolha", type=int)
    if selected < 1 or selected > len(choices):
        raise typer.BadParameter("Escolha de branch inválida")
    return choices[selected - 1]


@app.command()
def watch() -> None:
    """Opera sequencialmente e atravessa quotas com retry confiável."""
    try:
        SupervisorService.from_config(load_config()).watch()
    except KeyboardInterrupt:
        typer.echo("Supervisor interrompido; checkpoints preservados.")
    except (
        ConfigurationError,
        ExecutionStoreError,
        ResumeError,
        RunPipelineError,
        WorkError,
        SupervisorError,
    ) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error


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
    if record.quota_provider:
        typer.echo(f"Provider em espera: {record.quota_provider}")
        typer.echo(f"Classificação: {record.quota_classification}")
        typer.echo(
            "Próxima tentativa: "
            + (record.quota_retry_at.isoformat() if record.quota_retry_at else "não informada")
        )
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
    typer.echo(f"Correções: {result.correction_attempts}")


@app.command()
def work() -> None:
    """Retoma uma execução ou executa a próxima Issue Ready elegível."""
    try:
        result = WorkService.from_config(load_config()).work()
    except (
        ConfigurationError,
        ExecutionStoreError,
        ResumeError,
        RunPipelineError,
        WorkError,
    ) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    if result is None:
        typer.echo("Nenhuma Issue Ready elegível.")
        return
    if result.resumed:
        resumed = result.resume
        if resumed is None:
            raise typer.Exit(code=1)
        typer.echo(f"Execução existente retomada: {resumed.execution_id}")
        typer.echo(f"Issue: #{resumed.issue_number}")
        typer.echo(f"Branch: {resumed.branch or '-'}")
        pr = f"#{resumed.pull_request_number}" if resumed.pull_request_number else "-"
        typer.echo(f"PR: {pr} {resumed.pull_request_url or ''}".rstrip())
        typer.echo(f"CI: {resumed.ci_status or '-'}")
        typer.echo(f"Gemini: {resumed.review_verdict or '-'}")
        typer.echo(f"Fase: {resumed.phase}")
        typer.echo(f"Correções: {resumed.correction_attempts}")
        typer.echo(f"Merge: {resumed.merge_status}")
        typer.echo(f"Project status: {resumed.project_status or '-'}")
        return
    run_result = result.run
    if run_result is None:
        raise typer.Exit(code=1)
    typer.echo(f"Issue selecionada: #{run_result.issue_number}")
    typer.echo(f"Branch: {run_result.branch}")
    typer.echo(
        f"PR: #{run_result.pull_request_number} {run_result.pull_request_url}".rstrip()
    )
    typer.echo(f"CI: {run_result.ci_status or '-'}")
    typer.echo(
        f"Gemini: {run_result.review.verdict if run_result.review else '-'}"
    )
    typer.echo(f"Correções: {run_result.correction_attempts}")
    typer.echo(f"Merge: {run_result.merge_status}")
    typer.echo(f"Project status: {run_result.project_status}")


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
