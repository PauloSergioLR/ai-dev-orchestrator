"""Diagnóstico local dos pré-requisitos do orquestrador."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from typing import Sequence

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter, AntigravityError
from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexError
from ai_dev_orchestrator.adapters.github import (
    GitHubProjectAdapter,
    GitHubProjectError,
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
from ai_dev_orchestrator.services.review import (
    REVIEW_PLAN_SCHEMA,
    STRUCTURED_REVIEW_SCHEMA,
    ReviewError,
    parse_review_plan,
    parse_structured_review,
)
from ai_dev_orchestrator.domain.project_contract import ContractConfidence
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver


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
        checks = [
            self._check_python(),
            self._check_command("Git", ["git", "--version"]),
            self._check_github_cli(),
            self._check_command("Codex CLI", ["codex", "--version"]),
            self._check_antigravity_cli(),
            self._check_repository(),
            self._check_configuration(),
        ]
        checks.extend(self._check_project_contract())
        if not deep:
            return checks
        checks.extend(self._deep_provider_checks())
        if state:
            checks.append(self._check_state_consistency())
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
            contract = ProjectCapabilityResolver().resolve(
                root,
                repository_identity=config.github.repository_full_name,
                base_branch=config.workspace.base_ref,
                pull_request_target=config.github.pull_request_base,
                protected_branches=config.github.protected_branches,
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
        repository = self.runner.run(["git", "rev-parse", "--is-inside-work-tree"])
        if repository.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, repository.error)
        if not repository.succeeded or repository.stdout.strip() != "true":
            return DoctorCheck("Repository", CheckStatus.ERROR, "diretório não é um repositório Git")

        remote = self.runner.run(["git", "remote"])
        if remote.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, remote.error)
        if not remote.succeeded:
            return DoctorCheck("Repository", CheckStatus.ERROR, self._command_failure(remote))
        if not remote.stdout.strip():
            return DoctorCheck("Repository", CheckStatus.ERROR, "nenhum remote configurado")
        return DoctorCheck("Repository", CheckStatus.OK, "remote configurado")

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
