"""Interface de linha de comando do AI Dev Orchestrator."""

import json
from dataclasses import replace
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
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.services.cleanup import CleanupService
from ai_dev_orchestrator.services.history import HistoryService, format_duration
from ai_dev_orchestrator.services.supersession import SupersessionError, SupersessionService
from ai_dev_orchestrator.services.inspect import InspectService, Inspection
from ai_dev_orchestrator.domain.project import ProjectStatusOption, infer_status_mapping
from ai_dev_orchestrator.services.project_discovery import (
    AiProjectContractInterpreter,
    ProjectCapabilityResolver,
)

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
def doctor(
    deep: bool = typer.Option(
        False, "--deep",
        help="Executa probes reais e sintéticos dos providers; pode consumir quota/tokens.",
    ),
    state: bool = typer.Option(
        False, "--state",
        help="Com --deep, compara SQLite, PRs e Project somente em leitura.",
    ),
) -> None:
    """Diagnostica pré-requisitos locais; --deep valida providers por intenção explícita."""
    if state and not deep:
        raise typer.BadParameter("--state exige --deep")
    if deep:
        typer.echo(
            "Aviso: --deep envia prompts sintéticos aos providers e pode consumir quota/tokens. "
            "Não usa Issue, PR ou sessão de produção.\n"
        )
    checks = (
        DoctorService().diagnose(deep=True, state=state)
        if deep else DoctorService().diagnose()
    )

    typer.echo("AI Dev Orchestrator Doctor\n")
    for check in checks:
        scope = f"{check.scope.value:<18} " if deep else ""
        typer.echo(f"{scope}{check.name:<30} {check.status.value:<7} {check.message}")

    if has_errors(checks):
        raise typer.Exit(code=1)


@app.command("init")
def init_project(
    notifications: bool = typer.Option(False, "--notifications", help="Pergunta quais canais operacionais configurar."),
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
    if found.contract is not None and found.contract.ambiguities:
        try:
            from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter

            interpreter = AiProjectContractInterpreter(
                AntigravityAdapter(120), found.repository_path
            )
            interpreted = ProjectCapabilityResolver(interpreter).resolve(
                found.repository_path,
                repository_identity=found.contract.repository_identity,
                base_branch=found.contract.base_branch,
                pull_request_target=found.contract.pull_request_target,
                protected_branches=found.contract.protected_branches,
            )
            if not interpreted.ambiguities:
                found = replace(found, contract=interpreted)
                typer.echo("A IA resolveu a ambiguidade com evidências versionadas verificadas.")
        except Exception as error:
            typer.echo(f"IA não resolveu o contrato com segurança: {error}")
    discovered_gates: list[dict[str, object]] = []
    if found.contract is not None:
        typer.echo("\nContrato operacional descoberto")
        typer.echo(f"- confiança: {found.contract.confidence}")
        typer.echo(f"- fingerprint: {found.contract.fingerprint}")
        typer.echo(
            "- gates: "
            + (", ".join(gate.display_name for gate in found.contract.gates) or "nenhum")
        )
        typer.echo("- CI esperada: " + (", ".join(found.contract.expected_ci) or "não comprovada"))
        for operation in found.contract.excluded_operations:
            typer.echo(f"- não automática ({operation.risk_class}): {operation.display_name}")
        if found.contract.ambiguities:
            typer.echo("Contrato de validação ambíguo: " + "; ".join(found.contract.ambiguities))
            raw_argv = typer.prompt(
                "Informe uma única vez o argv do gate como JSON (ex.: [\"tools/validate\", \"--all\"])"
            )
            try:
                argv = json.loads(raw_argv)
            except json.JSONDecodeError as error:
                raise typer.BadParameter("argv deve ser um array JSON válido") from error
            if not isinstance(argv, list) or not argv or any(not isinstance(part, str) or not part for part in argv):
                raise typer.BadParameter("argv deve ser um array JSON de textos não vazios")
            discovered_gates.append({"name": "validation-override", "argv": tuple(argv)})
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
    if not existing and found.evidence and found.suggested_base_branch:
        base = found.suggested_base_branch
        target = base
        typer.echo(f"Base e destino comprovados pela documentação: {base}")
    else:
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
    inferred_status_mapping: dict[str, str] = {}
    if found.contract is not None and not (existing and existing.github.status_mapping):
        option_names = service.discover_status_options(owner, project_number, found.repository_path)
        inferred = infer_status_mapping(tuple(
            ProjectStatusOption(str(index), name) for index, name in enumerate(option_names)
        ))
        if inferred is not None:
            inferred_status_mapping = inferred
            typer.echo("Mapeamento semântico do Status: " + ", ".join(
                f"{logical} → {visual}" for logical, visual in inferred.items()
            ))
        elif option_names:
            typer.echo("Opções de Status ambíguas: " + ", ".join(option_names))
            raw_mapping = typer.prompt("Mapeamento semântico como objeto JSON")
            try:
                parsed_mapping = json.loads(raw_mapping)
            except json.JSONDecodeError as error:
                raise typer.BadParameter("mapeamento deve ser um objeto JSON válido") from error
            if not isinstance(parsed_mapping, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                or value not in option_names for key, value in parsed_mapping.items()
            ):
                raise typer.BadParameter("mapeamento usa estado lógico ou opção visual inválida")
            inferred_status_mapping = parsed_mapping
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
            "pull_request_target": target, "protected_branches": protected,
            "status_field_name": existing.github.status_field_name if existing else "Status",
            "status_mapping": existing.github.status_mapping if existing else inferred_status_mapping,
        },
        "workspace": {
            "repository_path": found.repository_path,
            "worktrees_dir": existing.workspace.worktrees_dir if existing else found.repository_path.parent / f"{found.repository_path.name}-worktrees",
            "base_branch": base, "remote_name": remote_name,
        },
        "providers": {"codex_model": codex_model, "gemini_model": gemini_model},
        "execution": existing.execution.model_dump() if existing else {"max_attempts": 2, "max_parallel_runs": 1, "auto_merge": False},
        "state": existing.state.model_dump() if existing else {},
        "ci": existing.ci.model_dump(exclude_unset=True) if existing else {},
        "convergence": existing.convergence.model_dump() if existing else {},
        "review": existing.review.model_dump() if existing else {},
        "supervisor": existing.supervisor.model_dump() if existing else {},
        "notifications": existing.notifications.model_dump() if existing else {},
        "project": existing.project.model_dump() if existing else {"gates": discovered_gates},
    }
    if notifications and typer.confirm("Deseja configurar notificações operacionais?", default=True):
        from ai_dev_orchestrator.adapters.notifications import missing_environment
        channels = tuple(value.strip() for value in typer.prompt("Canais separados por vírgula (email, discord, telegram)", default="email").split(",") if value.strip())
        from ai_dev_orchestrator.config import NotificationConfig
        try:
            values["notifications"] = NotificationConfig(channels=channels).model_dump()
        except ValueError as error:
            raise typer.BadParameter("Canais válidos: email, discord, telegram") from error
        missing = missing_environment(channels)
        typer.echo("Credenciais são lidas do ambiente; não serão gravadas no TOML.")
        if missing:
            typer.echo("Variáveis ausentes: " + ", ".join(missing))
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
        f"checks={', '.join(values['ci'].get('required_checks', ())) or 'automático'}; "
        f"worktrees={values['workspace']['worktrees_dir']}"
    )
    typer.confirm("Salvar configuração?", default=True, abort=True)
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
    if record.human_reason:
        typer.echo(f"Motivo humano: {record.human_reason} — {record.last_error}")
        typer.echo(f"Fase interrompida: {record.human_phase}; horário: {record.human_at}")
        deliveries = SqliteExecutionStore(load_config().state.database_path).notification_deliveries(record.id)
        for delivery in deliveries:
            typer.echo(f"Entrega {delivery['channel']}: {delivery['status']} ({delivery['attempts']} tentativas)")
    if record.quota_provider:
        typer.echo(f"Provider em espera: {record.quota_provider}")
        typer.echo(f"Classificação: {record.quota_classification}")
        typer.echo(
            "Próxima tentativa: "
            + (record.quota_retry_at.isoformat() if record.quota_retry_at else "não informada")
        )
        if record.provider_resume_phase:
            typer.echo(f"Fase suspensa: {record.provider_resume_phase}")
            typer.echo(f"Falhas consecutivas: {record.provider_retry_attempts}")
            typer.echo(f"Diagnóstico: {record.last_error or '-'}")
    typer.echo(f"Atualizado em: {record.updated_at.isoformat()}")


@app.command()
def inspect(
    issue: int = typer.Option(..., "--issue", min=1, help="Número positivo da Issue."),
    as_json: bool = typer.Option(False, "--json", help="Emite o diagnóstico em JSON estável."),
) -> None:
    """Diagnostica uma execução local sem alterar SQLite, providers ou Git."""
    try:
        diagnosis = InspectService.from_database(load_config().state.database_path).inspect(issue)
    except (ConfigurationError, ExecutionStoreError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    if diagnosis is None:
        typer.echo(f"Nenhuma execução encontrada para a Issue #{issue}.", err=True)
        raise typer.Exit(code=1)
    if as_json:
        typer.echo(json.dumps(diagnosis.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    _show_inspection(diagnosis)


def _show_inspection(diagnosis: Inspection) -> None:
    """Renderização humana do mesmo contrato usado pela automação."""
    typer.echo(f"Issue: #{diagnosis.issue} | execução: {diagnosis.execution_id}")
    typer.echo(f"Fase: {diagnosis.phase} | terminal: {'sim' if diagnosis.terminal else 'não'}")
    typer.echo(f"Branch: {diagnosis.branch or '-'} | worktree: {diagnosis.worktree_path or '-'} | base: {diagnosis.base_ref or '-'}")
    typer.echo(f"Sessão Codex: {diagnosis.codex_session_id or '-'} | modelos: Codex={diagnosis.models['codex']}, Gemini={diagnosis.models['gemini']}")
    pr = diagnosis.pull_request
    typer.echo(f"PR: #{pr['number'] or '-'} | URL: {pr['url'] or '-'}")
    heads = diagnosis.heads
    typer.echo("HEADs: current={current} | ci={ci} | reviewed={reviewed} | merged={merged} | merge commit={merge_commit}".format(**{key: value or '-' for key, value in heads.items()}))
    typer.echo(f"Review: {diagnosis.review['verdict'] or '-'} | correções: {diagnosis.review['correction_attempts']}")
    typer.echo(
        f"Repositório: {diagnosis.repository_identity or '-'} | contrato: "
        f"{diagnosis.contract['fingerprint'] or '-'}"
    )
    typer.echo(
        "Correções: locais={local_gates} | CI={ci} | review={review}".format(
            **diagnosis.corrections
        )
    )
    for gate in diagnosis.gates:
        typer.echo(
            f"Gate: {gate.get('name', '-')} | {gate.get('category', '-')} | "
            f"resultado={gate.get('succeeded')} | duração={gate.get('duration_seconds', 0)}s"
        )
    quota = diagnosis.quota
    typer.echo("Quota: provider={provider} | classificação={classification} | observado={observed_at} | retry={retry_at}".format(**{key: value or '-' for key, value in quota.items()}))
    human = diagnosis.human_required
    typer.echo("Intervenção humana: motivo={reason} | fase={phase} | horário={at}".format(**{key: value or '-' for key, value in human.items()}))
    typer.echo(f"Erro final: {diagnosis.last_error or '-'}")
    typer.echo(f"Project: {diagnosis.project_status or '-'} | cleanup: {diagnosis.cleanup['status']} ({diagnosis.cleanup['detail'] or '-'})")
    typer.echo("Findings do HEAD atual:")
    for finding in diagnosis.findings:
        typer.echo("- {severity}: {title} | {path}:{line} | {criterion}".format(**{key: value or '-' for key, value in finding.items()}))
    if not diagnosis.findings:
        typer.echo("- nenhum")
    typer.echo("Últimos eventos:")
    for event in diagnosis.events:
        typer.echo("- #{sequence} {phase} ({created_at}): {summary}".format(**event))
    if diagnosis.inconsistencies:
        typer.echo("Inconsistências:")
        for message in diagnosis.inconsistencies:
            typer.echo(f"- {message}")


@app.command()
def history(
    issue: int | None = typer.Option(None, "--issue", min=1, help="Filtra por Issue."),
) -> None:
    """Exibe execuções e métricas derivadas do journal SQLite local."""
    try:
        config = load_config()
        entries = HistoryService(SqliteExecutionStore(config.state.database_path)).list(issue)
    except (ConfigurationError, ExecutionStoreError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not entries:
        typer.echo("Nenhuma execução encontrada.")
        return
    for entry in entries:
        run = entry.run
        pr = f"#{run.pull_request_number}" if run.pull_request_number else "-"
        reason = run.last_error or "-"
        tokens = _usage_text(run.codex_tokens, run.gemini_tokens, run.codex_cost, run.gemini_cost)
        typer.echo(
            f"#{run.issue_number} | {run.id} | {run.phase} | duração {format_duration(entry.duration)} | "
            f"branch {run.branch or '-'} | PR {pr} | correções {run.correction_attempts} | "
            f"reviews {entry.reviews} | CI {format_duration(entry.ci_wait)} | quota {format_duration(entry.quota_wait)} | "
            f"Codex {run.codex_model}; Gemini {run.gemini_model} | merge {run.merge_commit_sha or '-'} | "
            f"Project {run.project_status or '-'} | repo {run.repository_identity or '-'} | "
            f"contrato {run.contract_fingerprint or '-'} | correções locais {run.local_gate_correction_attempts} | "
            f"correções CI {run.ci_correction_attempts} | cleanup {run.cleanup_status} | tokens {tokens} | motivo {reason}"
        )


@app.command()
def cleanup(
    issue: int = typer.Option(..., "--issue", min=1, help="Issue cuja execução concluída será limpa."),
) -> None:
    """Solicita cleanup seguro de uma execução concluída, conforme a política local."""
    try:
        config = load_config()
        store = SqliteExecutionStore(config.state.database_path)
        record = store.get_latest_for_issue(issue)
        if record is None:
            raise ExecutionStoreError(f"Nenhuma execução encontrada para a Issue #{issue}.")
        result = CleanupService(config, store, GitWorktreeAdapter()).cleanup(record.id)
    except (ConfigurationError, ExecutionStoreError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Cleanup {result.status}: {result.detail}")


def _usage_text(codex: int | None, gemini: int | None, codex_cost: float | None, gemini_cost: float | None) -> str:
    if codex is None and gemini is None and codex_cost is None and gemini_cost is None:
        return "indisponível"
    values = []
    if codex is not None:
        values.append(f"Codex={codex}")
    if gemini is not None:
        values.append(f"Gemini={gemini}")
    if codex_cost is not None:
        values.append(f"custo Codex={codex_cost}")
    if gemini_cost is not None:
        values.append(f"custo Gemini={gemini_cost}")
    return ", ".join(values)


@app.command()
def resume(
    issue: int = typer.Option(..., "--issue", min=1, help="Número positivo da Issue."),
    retry_provider: bool = typer.Option(False, "--retry-provider", help="Tenta novamente após intervenção na causa do bloqueio do provider."),
    recover_failed: bool = typer.Option(False, "--recover-failed", help="Reconcilia FAILED transitório com provas locais/remotas antes de reativar."),
) -> None:
    """Retoma uma execução ativa a partir do estado persistido."""
    try:
        service = ResumeService.from_config(load_config())
        options = {}
        if retry_provider:
            options["retry_provider"] = True
        if recover_failed:
            options["recover_failed"] = True
        result = service.resume(issue, **options)
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
def supersede(
    issue: int = typer.Option(..., "--issue", min=1, help="Issue cuja execução antiga será supersedida."),
    reason: str = typer.Option(..., "--reason", help="Motivo humano, persistido de forma sanitizada."),
    yes: bool = typer.Option(False, "--yes", help="Confirma sem prompt interativo."),
) -> None:
    """Marca um run com PR fechado sem merge como deliberadamente supersedido."""
    try:
        service = SupersessionService.from_config(load_config())
        preview = service.preview(issue)
        typer.echo("Evidência observada: " + preview.evidence)
        if not yes and not typer.confirm("Superseder esta execução sem alterar PR, Project ou arquivos?"):
            typer.echo("Supersessão cancelada; nenhuma alteração foi feita.")
            return
        result = service.supersede(issue, reason)
    except (ConfigurationError, ExecutionStoreError, SupersessionError) as error:
        typer.echo(f"Erro: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Execução {result.id} supersedida; histórico preservado.")


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
