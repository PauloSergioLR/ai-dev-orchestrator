"""Diagnóstico local dos pré-requisitos do orquestrador."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from tempfile import TemporaryDirectory
import tomllib
from typing import Sequence

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter, AntigravityError
from ai_dev_orchestrator.adapters.notifications import configuration_error, missing_environment
from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexError
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter, GitWorktreeError
from ai_dev_orchestrator.adapters.github import (
    GitHubProjectAdapter,
    GitHubProjectError,
    GitHubProjectStatusAdapter,
    GitHubProjectStatusError,
    GitHubPullRequestAdapter,
    GitHubPullRequestError,
)
from ai_dev_orchestrator.config import ConfigurationError, load_config
from ai_dev_orchestrator.domain.provider import ProviderFailure
from ai_dev_orchestrator.infrastructure.database import (
    ExecutionStoreError,
    SqliteExecutionStore,
    sanitize_diagnostic_text,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.infrastructure.codex_runtime import codex_candidates, global_codex_settings
from ai_dev_orchestrator.services.review import (
    REVIEW_PLAN_SCHEMA,
    STRUCTURED_REVIEW_SCHEMA,
    ReviewError,
    parse_review_plan,
    parse_structured_review,
)
from ai_dev_orchestrator.domain.project_contract import (
    CommandPlan, ContractConfidence, SourceEvidence,
)
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver
from ai_dev_orchestrator.services.code_review_graph import CodeReviewGraphIntegrator


class CheckStatus(StrEnum):
    """Estado de uma verificação do diagnóstico."""

    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


class CheckScope(StrEnum):
    """Origem da evidência apresentada pelo doctor."""

    LOCAL_CAPABILITY = "LOCAL_CAPABILITY"
    LIVE_PROVIDER = "LIVE_PROVIDER"
    STATE_CONSISTENCY = "STATE_CONSISTENCY"


@dataclass(frozen=True)
class DoctorCheck:
    """Resultado estruturado de uma verificação."""

    name: str
    status: CheckStatus
    message: str
    scope: CheckScope = CheckScope.LOCAL_CAPABILITY

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", sanitize_diagnostic_text(self.message) or "")


class DoctorService:
    """Agrupa verificações locais e sem efeitos colaterais."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.config_path = config_path

    def diagnose(self, *, deep: bool = False, state: bool = False) -> list[DoctorCheck]:
        """Executa todas as verificações obrigatórias do comando doctor."""
        codex_cli = self._check_codex_identity()
        github_cli = self._check_github_cli()
        local_permissions = self._check_local_permissions()
        checks = [
            self._check_python(),
            self._check_command("Git", ["git", "--version"]),
            github_cli,
            codex_cli,
            self._check_antigravity_cli(),
            self._check_repository(),
            self._check_configuration(),
            self._check_codex_model_source(),
            self._check_github_project(github_cli),
            self._summarize_local_permissions(codex_cli, local_permissions),
            *local_permissions,
        ]
        checks.extend(self._check_code_review_graph())
        checks.extend(self._check_notifications())
        checks.extend(self._check_project_contract())
        if not deep:
            return checks
        checks.extend(self._deep_provider_checks())
        if state:
            checks.append(self._check_state_consistency())
        return checks

    def _check_codex_identity(self) -> DoctorCheck:
        """Mostra o comando escolhido pelo PATH e compara candidatos sem abrir provider."""
        candidates = codex_candidates()
        if not candidates:
            return DoctorCheck("Codex CLI", CheckStatus.ERROR, "codex não encontrado no PATH")
        reports: list[str] = []
        versions: list[str | None] = []
        for index, candidate in enumerate(candidates):
            # O CommandRunner recusa scripts batch do Windows por segurança; nesses
            # casos, caminho e origem seguem visíveis, mas a versão fica indisponível.
            command = ["codex", "--version"] if index == 0 else [candidate.path, "--version"]
            try:
                result = self.runner.run(command)
                version = result.stdout.strip() if result.succeeded else None
            except Exception:
                # Falha isolada de candidato secundário nunca derruba o diagnóstico.
                version = None
            versions.append(version)
            label = "usado" if index == 0 else f"candidato {index + 1}"
            display_path = candidate.path if index == 0 else self._compact_codex_path(candidate.path)
            reports.append(
                f"{label}: {display_path} ({candidate.origin}; versão {version or 'indisponível'})"
            )
        selected = versions[0]
        status = CheckStatus.OK if selected else CheckStatus.ERROR
        message = reports[0]
        if len(candidates) > 1:
            message += f"; aviso: {len(candidates)} candidatos no PATH"
            if selected and any(version and version != selected for version in versions[1:]):
                message += "; versões divergentes entre candidatos"
            if len(reports) > 1:
                message += "; outros: " + "; ".join(reports[1:])
        return DoctorCheck("Codex CLI", status, message)

    @staticmethod
    def _compact_codex_path(path: str) -> str:
        """Preserva os segmentos úteis de candidatos secundários."""
        normalized = path.replace("/", "\\")
        parts = [part for part in normalized.split("\\") if part]
        if len(parts) <= 3:
            return path
        return "…\\" + "\\".join(parts[-3:])

    def _check_codex_model_source(self) -> DoctorCheck:
        try:
            config = load_config(self.config_path)
        except ConfigurationError as error:
            return DoctorCheck("Codex model selection", CheckStatus.WARNING, self._safe_message(error))
        model = config.providers.codex_model
        if model != "default":
            return DoctorCheck(
                "Codex model selection", CheckStatus.OK,
                f"{model}; origem: orchestrator.toml",
            )
        global_model, effort, source = global_codex_settings()
        detail = "delegado ao Codex CLI/configuração global"
        if global_model:
            detail += f"; model={global_model}"
        if effort:
            detail += f"; model_reasoning_effort={effort}"
        if source and (global_model or effort):
            detail += "; chaves lidas de config.toml"
        return DoctorCheck("Codex model selection", CheckStatus.OK, detail)

    def _check_local_permissions(self) -> list[DoctorCheck]:
        """Prova escrita local sem iniciar provider nem alterar caminhos definitivos."""
        try:
            temporary_directory = Path(tempfile.gettempdir())
        except OSError as error:
            environment_checks = [DoctorCheck(
                "Temporary directory", CheckStatus.ERROR, self._safe_message(error),
            )]
        else:
            environment_checks = [self._probe_directory_write(
                "Temporary directory", temporary_directory, must_exist=True,
            )]
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            environment_checks.append(self._probe_directory_write(
                "uv cache write probe", Path(uv_cache).expanduser(), must_exist=False
            ))

        try:
            config = load_config(self.config_path)
        except ConfigurationError as error:
            return [DoctorCheck(
                "Configured path probes", CheckStatus.ERROR,
                "não executados porque a configuração é inválida: "
                f"{self._safe_message(error)}; impede uma execução normal",
            ), *environment_checks]

        return [
            self._probe_directory_write(
                "Workspace write probe",
                config.workspace.repository_path,
                must_exist=True,
            ),
            self._probe_directory_write(
                "Worktrees write probe",
                config.workspace.worktrees_dir,
                must_exist=False,
            ),
            self._probe_directory_write(
                "State directory",
                config.state.database_path.parent,
                must_exist=False,
            ),
            *environment_checks,
        ]

    @staticmethod
    def _summarize_local_permissions(
        codex_cli: DoctorCheck, checks: Sequence[DoctorCheck]
    ) -> DoctorCheck:
        failures = [
            check.name
            for check in (codex_cli, *checks)
            if check.status is CheckStatus.ERROR
        ]
        if failures:
            return DoctorCheck(
                "Codex local permissions", CheckStatus.ERROR,
                "falha bloqueante em " + ", ".join(failures)
                + "; corrija a permissão/ambiente antes de iniciar uma nova execução",
            )
        return DoctorCheck(
            "Codex local permissions", CheckStatus.OK,
            "executável e escrita local validados sem iniciar provider",
        )

    def _probe_directory_write(
        self, name: str, path: Path, *, must_exist: bool
    ) -> DoctorCheck:
        """Cria, lê e remove somente um artefato temporário de nome exclusivo."""
        target = path.absolute()
        artifact: Path | None = None
        probe_file: Path | None = None
        operation = "validar o caminho"
        failure: tuple[str, Path, Exception] | None = None
        target_missing = False

        try:
            try:
                target_info = target.stat()
            except FileNotFoundError:
                target_missing = True
            if not target_missing:
                if not stat.S_ISDIR(target_info.st_mode):
                    raise NotADirectoryError("o caminho existe, mas não é um diretório")
                probe_parent = target
            elif must_exist:
                raise FileNotFoundError("diretório obrigatório inexistente")
            else:
                probe_parent = self._nearest_existing_directory(target)

            operation = "criar diretório temporário exclusivo"
            artifact = Path(tempfile.mkdtemp(prefix=".orch-doctor-", dir=probe_parent))
            probe_file = artifact / "write-probe.txt"

            operation = "criar/escrever arquivo"
            probe_file.write_text("diagnóstico local: ação\n", encoding="utf-8")
            operation = "ler arquivo"
            if probe_file.read_text(encoding="utf-8") != "diagnóstico local: ação\n":
                raise OSError("conteúdo lido difere do conteúdo gravado")
        except (OSError, UnicodeError) as error:
            failure = (operation, target, error)
        finally:
            cleanup_failure = self._cleanup_write_probe(probe_file, artifact)
            if cleanup_failure is not None:
                failure = cleanup_failure

        if failure is not None:
            failed_operation, affected_path, error = failure
            return DoctorCheck(
                name,
                CheckStatus.ERROR,
                f"{failed_operation} falhou em {affected_path}: "
                f"{self._permission_cause(error)}; impede uma execução normal",
            )

        detail = f"escrita, leitura e limpeza validadas em {target}"
        if target_missing:
            detail += "; o diretório definitivo ainda não existe e poderá ser criado"
        return DoctorCheck(name, CheckStatus.OK, detail)

    @staticmethod
    def _nearest_existing_directory(path: Path) -> Path:
        candidate = path
        while True:
            try:
                candidate_info = candidate.stat()
            except FileNotFoundError:
                parent = candidate.parent
                if parent == candidate:
                    raise FileNotFoundError(
                        "nenhum diretório ancestral existente"
                    ) from None
                candidate = parent
                continue
            if not stat.S_ISDIR(candidate_info.st_mode):
                raise NotADirectoryError(
                    f"ancestral existente não é um diretório: {candidate}"
                )
            return candidate

    @staticmethod
    def _cleanup_write_probe(
        probe_file: Path | None, artifact: Path | None
    ) -> tuple[str, Path, Exception] | None:
        """Limpa apenas os dois caminhos criados pelo probe, sem remoção recursiva."""
        if artifact is None:
            return None
        if probe_file is not None:
            try:
                probe_file.unlink(missing_ok=True)
            except OSError as error:
                return "remover arquivo temporário", probe_file, error
        try:
            artifact.rmdir()
        except OSError as error:
            return "remover diretório temporário", artifact, error
        return None

    def _permission_cause(self, error: Exception) -> str:
        detail = self._safe_message(error)
        normalized = detail.casefold()
        access_denied = (
            isinstance(error, PermissionError)
            or getattr(error, "winerror", None) == 5
            or "access is denied" in normalized
            or "acesso negado" in normalized
        )
        return f"acesso negado ({detail})" if access_denied else detail

    def _check_notifications(self) -> list[DoctorCheck]:
        """Valida apenas configuração local; nunca revela nem transmite secrets."""
        try:
            config = load_config(self.config_path)
        except ConfigurationError:
            return []
        policy = config.notifications
        if not policy.enabled:
            return [DoctorCheck("Notificações", CheckStatus.OK, "desabilitadas globalmente")]
        if not policy.channels:
            return [DoctorCheck("Notificações", CheckStatus.OK, "nenhum provider habilitado")]
        checks: list[DoctorCheck] = []
        for channel in policy.channels:
            enabled = getattr(policy, f"{channel}_enabled", True)
            if not enabled:
                checks.append(DoctorCheck(f"Notificação {channel}", CheckStatus.OK, "desabilitada na configuração"))
                continue
            absent = missing_environment((channel,))
            if absent:
                checks.append(DoctorCheck(f"Notificação {channel}", CheckStatus.WARNING, "variáveis ausentes: " + ", ".join(absent)))
            elif error := configuration_error(channel):
                checks.append(DoctorCheck(f"Notificação {channel}", CheckStatus.WARNING, error))
            else:
                checks.append(DoctorCheck(f"Notificação {channel}", CheckStatus.OK, "configuração presente; use 'orch notifications test' para conectividade"))
        return checks

    def _check_project_contract(self) -> list[DoctorCheck]:
        """Mostra contrato e executáveis sem rodar gates nem consumir provider."""
        try:
            config = load_config(self.config_path)
        except ConfigurationError:
            return []
        root = config.workspace.repository_path
        # Mantém o diagnóstico de instalações ainda não inicializadas compatível.
        if not root.is_dir():
            return []
        try:
            overrides = tuple(
                CommandPlan(
                    gate.name, gate.capability, gate.name, gate.argv, gate.cwd,
                    gate.timeout_seconds, gate.required,
                    (SourceEvidence("orchestrator.toml", "explicit_override", gate.name),),
                    risk_class=ProjectCapabilityResolver._risk(" ".join(gate.argv)),
                )
                for gate in config.project.gates
            )
            contract = ProjectCapabilityResolver().resolve(
                root,
                repository_identity=config.github.repository_full_name,
                base_branch=config.workspace.base_ref,
                pull_request_target=config.github.pull_request_base,
                protected_branches=config.github.protected_branches,
                overrides=overrides,
            )
        except Exception as error:
            return [DoctorCheck("Contrato do projeto", CheckStatus.ERROR, self._safe_message(error))]
        status = CheckStatus.OK if contract.confidence is ContractConfidence.PROVEN else CheckStatus.ERROR
        summary = (
            f"{contract.confidence}; fingerprint={contract.fingerprint}; "
            f"base={contract.base_branch}; target={contract.pull_request_target}; "
            f"gates={', '.join(gate.name for gate in contract.gates) or 'nenhum'}; "
            f"CI={', '.join(contract.expected_ci) or 'não comprovada'}; "
            f"Status={config.github.status_mapping or 'mapeamento legado'}"
        )
        checks = [DoctorCheck("Contrato do projeto", status, summary)]
        for plan in (*contract.bootstrap, *contract.gates):
            local = (root / plan.cwd / plan.argv[0]).resolve()
            found = shutil.which(plan.argv[0]) is not None or (local.is_file() and root in local.parents)
            checks.append(DoctorCheck(
                f"Gate {plan.name}", CheckStatus.OK if found else CheckStatus.ERROR,
                f"cwd={plan.cwd}; timeout={plan.timeout_seconds:g}s; executável "
                + ("encontrado" if found else f"ausente: {plan.argv[0]}"),
            ))
        for plan in contract.excluded_operations:
            checks.append(DoctorCheck(
                f"Operação não automática {plan.name}", CheckStatus.WARNING,
                f"risco={plan.risk_class}; evidência={plan.source_evidence[0].path}",
            ))
        return checks

    def _check_code_review_graph(self) -> list[DoctorCheck]:
        """Valida pacote, grafo e configurações MCP sem alterá-los."""
        try:
            config = load_config(self.config_path)
        except ConfigurationError:
            return []
        crg = config.code_review_graph
        if not crg.enabled:
            return [DoctorCheck(
                "Code Review Graph", CheckStatus.OK,
                "desabilitado; habilite [code_review_graph] para usar a integração",
            )]

        integrator = CodeReviewGraphIntegrator(crg, self.runner)
        observed, error = integrator.version()
        if error:
            package = DoctorCheck(
                "Code Review Graph", CheckStatus.WARNING,
                f"indisponível ({error}); instale a versão {crg.required_version} "
                "ou ajuste code_review_graph.command; o pipeline fará fallback",
            )
        elif observed != crg.required_version:
            package = DoctorCheck(
                "Code Review Graph", CheckStatus.WARNING,
                f"versão incompatível: {observed}; esperada {crg.required_version}; "
                "instale a versão configurada",
            )
        else:
            package = DoctorCheck(
                "Code Review Graph", CheckStatus.OK, f"versão {observed} compatível"
            )

        root = config.workspace.repository_path
        stats, status_error = (
            integrator.status(root) if root.is_dir() and package.status is CheckStatus.OK
            else (None, "repositório ou pacote indisponível")
        )
        graph = DoctorCheck(
            "Grafo CRG",
            CheckStatus.OK if stats is not None else CheckStatus.WARNING,
            (
                f"íntegro; {stats['nodes']} nós, {stats['edges']} arestas, "
                f"{stats['files']} arquivos"
                if stats is not None else
                f"ausente ou inválido ({status_error}); será construído no primeiro uso"
            ),
        )
        return [
            package,
            graph,
            self._check_codex_mcp(),
            self._check_antigravity_mcp(),
        ]

    def _check_codex_mcp(self) -> DoctorCheck:
        path = Path.home() / ".codex" / "config.toml"
        if not path.exists():
            return DoctorCheck(
                "MCP CRG Codex", CheckStatus.OK,
                "configurado pelo orquestrador por execução e escopado ao worktree",
            )
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
            configured = self._valid_mcp_entry(
                data.get("mcp_servers", {}).get("code-review-graph")
            )
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            configured = False
        return DoctorCheck(
            "MCP CRG Codex",
            CheckStatus.OK if configured else CheckStatus.WARNING,
            "configuração encontrada" if configured else
            "config.toml existente é inválido; corrija-o ou execute "
            "code-review-graph install --platform codex",
        )

    def _check_antigravity_mcp(self) -> DoctorCheck:
        path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            configured = self._valid_mcp_entry(
                data.get("mcpServers", {}).get("code-review-graph")
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            configured = False
        return DoctorCheck(
            "MCP CRG Antigravity",
            CheckStatus.OK if configured else CheckStatus.WARNING,
            "configuração encontrada" if configured else
            "ausente ou inválida; o pipeline tentará configurá-la atomicamente antes "
            "do review; ou execute code-review-graph install --platform antigravity",
        )

    @staticmethod
    def _valid_mcp_entry(entry: object) -> bool:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            return False
        args = entry.get("args")
        return isinstance(args, list) and all(isinstance(arg, str) for arg in args) \
            and "serve" in args

    def _deep_provider_checks(self) -> list[DoctorCheck]:
        """Exercita providers somente em um diretório temporário descartável."""
        try:
            config = load_config(self.config_path)
        except ConfigurationError as error:
            return [
                DoctorCheck("Codex probe", CheckStatus.ERROR, self._safe_message(error), CheckScope.LIVE_PROVIDER),
                DoctorCheck("Antigravity probe", CheckStatus.ERROR, self._safe_message(error), CheckScope.LIVE_PROVIDER),
            ]

        with TemporaryDirectory(prefix="orch-doctor-") as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            codex = CodexAdapter(
                CommandRunner(timeout=60), model=config.providers.codex_model
            )
            checks, _ = self._probe_codex(codex, workspace)
            reviewer = AntigravityAdapter(
                config.review.timeout_seconds,
                model=config.providers.gemini_model,
                executable=config.review.executable,
            )
            checks.extend(self._probe_antigravity(reviewer, workspace))
        return checks

    def _probe_codex(
        self, adapter: CodexAdapter, workspace: Path
    ) -> tuple[list[DoctorCheck], str | None]:
        prompt = (
            "Diagnóstico sintético do orquestrador. Não crie, altere ou apague arquivos; "
            "responda somente com 'ok'. Verifique UTF-8: ação, revisão, çã."
        )
        try:
            execution = adapter.execute(workspace, prompt)
        except (CodexError, ProviderFailure) as error:
            return [
                self._provider_error("Codex exec", error),
                DoctorCheck(
                    "Codex resume", CheckStatus.WARNING,
                    "não executado porque a sessão sintética não foi criada",
                    CheckScope.LIVE_PROVIDER,
                ),
            ], None
        checks = [
            DoctorCheck(
                "Codex exec", CheckStatus.OK,
                "JSONL, UTF-8 e sessão sintética validados",
                CheckScope.LIVE_PROVIDER,
            )
        ]
        try:
            resumed = adapter.resume(workspace, execution.session_id, "Responda somente 'ok'; não altere arquivos.")
            if resumed.session_id != execution.session_id:
                raise CodexError("Codex retornou uma sessão diferente no probe de resume")
        except (CodexError, ProviderFailure) as error:
            checks.append(self._provider_error("Codex resume", error))
        else:
            checks.append(DoctorCheck(
                "Codex resume", CheckStatus.OK,
                "sessão sintética retomada sem usar sessão de produção",
                CheckScope.LIVE_PROVIDER,
            ))
        return checks, execution.session_id

    def _probe_antigravity(
        self, adapter: AntigravityAdapter, workspace: Path
    ) -> list[DoctorCheck]:
        plan_prompt = (
            "Diagnóstico sintético. Não execute comandos nem altere arquivos. "
            "Retorne estritamente o JSON do schema, com listas válidas; use UTF-8: ação."
        )
        checks = [self._probe_antigravity_schema(
            "Antigravity ReviewPlan", adapter, workspace, plan_prompt,
            REVIEW_PLAN_SCHEMA, parse_review_plan,
        )]
        sha = "0" * 40
        review_prompt = (
            "Diagnóstico sintético. Não execute comandos nem altere arquivos. "
            f"Retorne estritamente o JSON do schema com verdict APPROVED, findings [], "
            f"reviewed_head_sha {sha} e summary 'ação validada'."
        )
        checks.append(self._probe_antigravity_schema(
            "Antigravity StructuredReview", adapter, workspace, review_prompt,
            STRUCTURED_REVIEW_SCHEMA,
            lambda output: parse_structured_review(output, sha, ("CRITICAL", "HIGH", "MEDIUM")),
        ))
        return checks

    def _probe_antigravity_schema(self, name, adapter, workspace, prompt, schema, parser) -> DoctorCheck:
        try:
            output = adapter.invoke(prompt, workspace, schema)
            parser(output)
        except (AntigravityError, ProviderFailure, ReviewError) as error:
            return self._provider_error(name, error)
        return DoctorCheck(
            name, CheckStatus.OK,
            "structured_output real validado no schema de runtime",
            CheckScope.LIVE_PROVIDER,
        )

    def _check_state_consistency(self) -> DoctorCheck:
        """Compara apenas leituras locais e remotas; nunca reconcilia ou corrige."""
        try:
            config = load_config(self.config_path)
            store = SqliteExecutionStore(config.state.database_path, read_only=True)
            runs = store.list_active()
            projects = GitHubProjectAdapter(config).list_items()
            pull_requests = GitHubPullRequestAdapter(config)
        except (ConfigurationError, ExecutionStoreError, GitHubProjectError) as error:
            return DoctorCheck(
                "Estado SQLite/GitHub", CheckStatus.ERROR, self._safe_message(error),
                CheckScope.STATE_CONSISTENCY,
            )

        divergences: list[str] = []
        for run in runs:
            matching = [item for item in projects if item.issue_number == run.issue_number]
            if len(matching) != 1:
                divergences.append(f"Issue #{run.issue_number}: item do Project ausente ou ambíguo")
            elif run.project_status and matching[0].status != run.project_status:
                divergences.append(f"Issue #{run.issue_number}: status do Project diverge")
            if run.pull_request_number is not None:
                try:
                    remote = pull_requests.get_merge_snapshot(run.pull_request_number)
                except GitHubPullRequestError:
                    divergences.append(f"Issue #{run.issue_number}: PR #{run.pull_request_number} não pôde ser confirmado")
                    continue
                if run.current_head_sha and remote.head_sha != run.current_head_sha:
                    divergences.append(f"Issue #{run.issue_number}: HEAD do PR diverge")
        if divergences:
            return DoctorCheck(
                "Estado SQLite/GitHub", CheckStatus.WARNING,
                "; ".join(divergences[:5]) + ("; demais divergências omitidas" if len(divergences) > 5 else ""),
                CheckScope.STATE_CONSISTENCY,
            )
        return DoctorCheck(
            "Estado SQLite/GitHub", CheckStatus.OK,
            f"{len(runs)} execução(ões) ativa(s) conferida(s) somente em leitura",
            CheckScope.STATE_CONSISTENCY,
        )

    def _provider_error(self, name: str, error: Exception) -> DoctorCheck:
        if isinstance(error, ProviderFailure):
            message = f"{error.classification.value}: {self._safe_text(error.message)}"
        else:
            message = self._safe_message(error)
        return DoctorCheck(name, CheckStatus.ERROR, message, CheckScope.LIVE_PROVIDER)

    @staticmethod
    def _safe_message(error: Exception) -> str:
        return DoctorService._safe_text(str(error))

    @staticmethod
    def _safe_text(value: str) -> str:
        return (sanitize_diagnostic_text(value) or "falha sem diagnóstico")[:500]

    def _check_python(self) -> DoctorCheck:
        version = sys.version_info
        current = f"{version.major}.{version.minor}.{version.micro}"
        if version.major == 3 and version.minor == 13:
            return DoctorCheck("Python", CheckStatus.OK, current)
        return DoctorCheck(
            "Python", CheckStatus.ERROR, f"{current}; é necessário Python 3.13.x"
        )

    def _check_command(self, name: str, arguments: Sequence[str]) -> DoctorCheck:
        result = self.runner.run(arguments)
        if result.error:
            return DoctorCheck(name, CheckStatus.ERROR, result.error)
        if not result.succeeded:
            return DoctorCheck(name, CheckStatus.ERROR, self._command_failure(result))
        version = result.stdout.strip() or "disponível"
        return DoctorCheck(name, CheckStatus.OK, version)

    def _check_github_cli(self) -> DoctorCheck:
        result = self.runner.run(["gh", "auth", "status"])
        if result.error:
            return DoctorCheck("GitHub CLI", CheckStatus.ERROR, result.error)
        if not result.succeeded:
            return DoctorCheck("GitHub CLI", CheckStatus.ERROR, "gh não está autenticado")
        return DoctorCheck("GitHub CLI", CheckStatus.OK, "autenticado")

    def _check_github_project(self, github_cli: DoctorCheck | None = None) -> DoctorCheck:
        """Prova a leitura real do Project com a mesma operação usada pelo work."""
        if github_cli is not None and github_cli.status is CheckStatus.ERROR:
            return DoctorCheck(
                "GitHub Project", CheckStatus.ERROR,
                "não verificado porque o GitHub CLI não está autenticado/operacional",
            )
        try:
            config = load_config(self.config_path)
        except ConfigurationError as error:
            return DoctorCheck(
                "GitHub Project", CheckStatus.ERROR,
                "não verificado porque a configuração é inválida: "
                + self._safe_message(error),
            )

        project_runner = (
            CommandRunner(timeout=config.github.project_timeout_seconds)
            if isinstance(self.runner, CommandRunner)
            else self.runner
        )
        try:
            items = GitHubProjectAdapter(config, project_runner).list_items()
            status_adapter = GitHubProjectStatusAdapter(config, project_runner)
            status_field = status_adapter.resolve_status_field()
            required_statuses = {
                config.github.status_for(state) for state in (
                    "ready", "implementing", "waiting_ci", "ai_review", "human_required", "completed",
                )
            } | set(config.github.status_mapping.values())
            for name in sorted(required_statuses):
                status_adapter.resolve_status_option(status_field, name)
        except (GitHubProjectError, GitHubProjectStatusError) as error:
            return DoctorCheck(
                "GitHub Project", CheckStatus.ERROR,
                self._github_project_failure(
                    error,
                    project_number=config.github.project_number,
                    timeout_seconds=config.github.project_timeout_seconds,
                ),
            )

        override = self._github_token_override()
        credential = (
            f"; credencial efetiva sobrescrita por {override}"
            if override is not None else ""
        )
        return DoctorCheck(
            "GitHub Project", CheckStatus.OK,
            f"acesso read-only confirmado ao Project {config.github.project_number}; "
            f"{len(items)} item(ns); timeout={config.github.project_timeout_seconds:g}s"
            + credential,
        )

    def _github_project_failure(
        self, error: Exception, *, project_number: int, timeout_seconds: float
    ) -> str:
        detail = self._safe_message(error)
        normalized = detail.casefold()
        override = self._github_token_override()
        override_note = (
            f"; {override} está definido e sobrescreve a autenticação armazenada do gh"
            if override is not None else ""
        )

        if "timeout" in normalized or "excedeu o timeout" in normalized:
            return (
                f"{detail}; leitura do Project {project_number} excedeu o limite de "
                f"{timeout_seconds:g}s; ajuste github.project_timeout_seconds se necessário"
                + override_note
            )
        if "unknown owner type" in normalized:
            action = (
                f"; revise o owner e a autorização do token em {override}"
                if override is not None
                else "; confira github.owner e, se estiver correto, execute "
                "'gh auth refresh -h github.com -s project'"
            )
            return f"{detail}; owner não reconhecido pela credencial atual{action}"
        if "not found" in normalized or "could not resolve" in normalized:
            return (
                f"{detail}; confira github.owner e github.project_number"
                + override_note
            )
        auth_markers = (
            "insufficient scope", "insufficient scopes", "required scope",
            "authentication", "authorization", "forbidden", "permission",
        )
        if any(marker in normalized for marker in auth_markers):
            if override is not None:
                return (
                    f"{detail}; {override} está definido e deve possuir acesso ao GitHub Projects"
                )
            return (
                f"{detail}; a credencial do gh pode não possuir acesso ao GitHub Projects; "
                "ação sugerida: gh auth refresh -h github.com -s project"
            )
        return detail + override_note

    @staticmethod
    def _github_token_override() -> str | None:
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(name):
                return name
        return None

    def _check_antigravity_cli(self) -> DoctorCheck:
        """Usa a mesma configuração e o mesmo preflight do runtime."""
        try:
            config = load_config(self.config_path)
            version = AntigravityAdapter(
                config.review.timeout_seconds, runner=self.runner,
                model=config.providers.gemini_model,
                executable=config.review.executable,
            ).check_available()
        except (ConfigurationError, AntigravityError) as error:
            return DoctorCheck("Antigravity CLI", CheckStatus.ERROR, str(error))
        return DoctorCheck(
            "Antigravity CLI", CheckStatus.OK,
            version + "; contrato local validado (não consulta o modelo)",
        )

    def _check_repository(self) -> DoctorCheck:
        try:
            config = load_config(self.config_path)
        except ConfigurationError as error:
            return DoctorCheck("Repository", CheckStatus.ERROR, self._safe_message(error))
        repository = self.runner.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=config.workspace.repository_path,
        )
        if repository.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, repository.error)
        if not repository.succeeded or repository.stdout.strip() != "true":
            return DoctorCheck("Repository", CheckStatus.ERROR, "diretório não é um repositório Git")

        remote = self.runner.run(["git", "remote"], cwd=config.workspace.repository_path)
        if remote.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, remote.error)
        if not remote.succeeded:
            return DoctorCheck("Repository", CheckStatus.ERROR, self._command_failure(remote))
        if config.workspace.remote_name not in remote.stdout.splitlines():
            return DoctorCheck("Repository", CheckStatus.ERROR, "remote configurado não existe no repositório")
        try:
            GitWorktreeAdapter(self.runner).verify_remote_identity(
                config.workspace.repository_path, config.workspace.remote_name, config.github.repository_full_name,
            )
        except GitWorktreeError as error:
            return DoctorCheck("Repository", CheckStatus.ERROR, str(error))
        return DoctorCheck("Repository", CheckStatus.OK, "repositório e remote configurados confirmados")

    def _check_configuration(self) -> DoctorCheck:
        try:
            load_config(self.config_path)
        except ConfigurationError as error:
            return DoctorCheck("Configuration", CheckStatus.ERROR, str(error))
        return DoctorCheck("Configuration", CheckStatus.OK, "orchestrator.toml válido")

    @staticmethod
    def _command_failure(result: CommandResult) -> str:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            return f"comando retornou código {result.returncode}: {detail}"
        return f"comando retornou código {result.returncode}"


def has_errors(checks: Sequence[DoctorCheck]) -> bool:
    """Indica se algum resultado impede a execução do ambiente."""
    return any(check.status is CheckStatus.ERROR for check in checks)
