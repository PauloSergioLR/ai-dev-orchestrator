"""Validações locais independentes executadas após o Codex."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from time import monotonic
from collections.abc import Callable
from typing import Mapping, Protocol, Sequence

from ai_dev_orchestrator.domain.project_contract import CommandPlan, ProjectContract, RiskClass
from ai_dev_orchestrator.domain.provider import ProviderFailure, FAILURE_MESSAGES, classify_process_failure
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.infrastructure.database import sanitize_diagnostic_text

MAX_GATE_DIAGNOSTIC_CHARACTERS = 500


class LocalFailureKind(StrEnum):
    PROJECT_TEST_FAILURE = "PROJECT_TEST_FAILURE"
    PROJECT_BUILD_FAILURE = "PROJECT_BUILD_FAILURE"
    DISCOVERY_ERROR = "DISCOVERY_ERROR"
    INVALID_GATE = "INVALID_GATE"
    MISSING_REQUIRED_ENVIRONMENT = "MISSING_REQUIRED_ENVIRONMENT"
    LOCAL_INFRASTRUCTURE = "LOCAL_INFRASTRUCTURE"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"


class LocalValidationError(Exception):
    """Indica que um gate local obrigatório falhou."""

    def __init__(
        self,
        message: str,
        *,
        result: "GateResult | None" = None,
        kind: LocalFailureKind | None = None,
        correctable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.kind = kind or (
            LocalFailureKind.PROJECT_TEST_FAILURE
            if result is not None
            else LocalFailureKind.INVALID_GATE
        )
        self.correctable = result is not None if correctable is None else correctable


class LocalProcessFailure(ProviderFailure, LocalValidationError):
    """Falha de infraestrutura distinta de um teste que efetivamente falhou."""


class ProcessRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        cwd: str | Path | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class GateResult:
    name: str
    command: tuple[str, ...]
    succeeded: bool
    returncode: int | None
    diagnostic: str
    duration_seconds: float = 0
    category: str = "project-validation"


class LocalValidationService:
    """Executa o plano recebido, em ordem, sem interpretar comandos por shell."""

    def __init__(self, runner: ProcessRunner | None = None,
                 plans: Sequence[CommandPlan] | None = None,
                 progress: Callable[[str], None] | None = None) -> None:
        self.runner = runner
        self.plans = tuple(plans) if plans is not None else None
        self.progress = progress

    def validate(self, worktree: str | Path, contract: ProjectContract | None = None) -> tuple[GateResult, ...]:
        configured = self.plans or (() if contract is None else (*contract.bootstrap, *contract.gates))
        plans = tuple(plan for plan in configured if plan.required)
        if not plans:
            raise LocalValidationError(
                "Contrato não contém gates locais executáveis",
                kind=LocalFailureKind.DISCOVERY_ERROR,
            )
        root = Path(worktree).resolve()
        results: list[GateResult] = []
        for plan in plans:
            if plan.risk_class is not RiskClass.SAFE_LOCAL:
                raise LocalValidationError(
                    f"Gate '{plan.name}' não é uma operação local segura",
                    kind=LocalFailureKind.INVALID_GATE,
                )
            cwd = (root / plan.cwd).resolve()
            if cwd != root and root not in cwd.parents:
                raise LocalValidationError(
                    f"cwd do gate '{plan.name}' escapa do worktree",
                    kind=LocalFailureKind.INVALID_GATE,
                )
            if not cwd.is_dir():
                raise LocalValidationError(
                    f"cwd do gate '{plan.name}' não existe: {plan.cwd}",
                    kind=LocalFailureKind.INVALID_GATE,
                )
            if self.progress:
                self.progress(f"Gate local iniciado: {plan.name}")
            started = monotonic()
            temporary_root: Path | None = None
            try:
                if self._is_pytest_gate(plan.argv):
                    temporary_root = Path(tempfile.mkdtemp(prefix=".orch-gate-", dir=root))
                environment = self._gate_environment(plan.argv, temporary_root)
                runner = self.runner or CommandRunner(timeout=plan.timeout_seconds)
                result = runner.run(plan.argv, cwd=cwd, environment=environment)
            except OSError as error:
                raise LocalValidationError(
                    f"Infraestrutura local do gate '{plan.name}' falhou: "
                    f"{type(error).__name__}",
                    kind=LocalFailureKind.LOCAL_INFRASTRUCTURE,
                    correctable=False,
                ) from error
            finally:
                if temporary_root is not None:
                    try:
                        shutil.rmtree(temporary_root)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        raise LocalValidationError(
                            f"Infraestrutura local do gate '{plan.name}' não pôde "
                            "remover o temporário isolado que criou",
                            kind=LocalFailureKind.LOCAL_INFRASTRUCTURE,
                            correctable=False,
                        ) from error
            duration = monotonic() - started
            if result.failure_kind is not None:
                kind = classify_process_failure(result.failure_kind)
                raise LocalProcessFailure(
                    "local", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                    returncode=result.returncode, diagnostic_source="processo",
                )
            raw_diagnostic = result.error or result.stderr.strip() or result.stdout.strip()
            diagnostic = self._summarize(raw_diagnostic)
            gate = GateResult(plan.name, plan.argv, result.succeeded, result.returncode, diagnostic, duration, plan.capability)
            results.append(gate)
            if self.progress:
                outcome = "aprovado" if gate.succeeded else "reprovado"
                self.progress(f"Gate local {outcome}: {plan.name}")
            if not gate.succeeded:
                detail = f": {diagnostic}" if diagnostic else ""
                missing_environment = bool(re.search(
                    r"(?i)(environment variable|vari[aá]vel de ambiente|not set|undefined variable|missing env)",
                    diagnostic,
                ))
                local_infrastructure = self._is_local_infrastructure_failure(
                    raw_diagnostic, temporary_root
                )
                kind = (
                    LocalFailureKind.LOCAL_INFRASTRUCTURE
                    if local_infrastructure
                    else LocalFailureKind.MISSING_REQUIRED_ENVIRONMENT
                    if missing_environment
                    else LocalFailureKind.PROJECT_BUILD_FAILURE
                    if "build" in plan.capability.casefold()
                    else LocalFailureKind.PROJECT_TEST_FAILURE
                )
                raise LocalValidationError(
                    f"Gate local '{plan.name}' falhou{detail}",
                    result=gate,
                    kind=kind,
                    correctable=not (missing_environment or local_infrastructure),
                )
        return tuple(results)

    @staticmethod
    def _gate_environment(
        arguments: Sequence[str],
        temporary_root: Path | None = None,
        *,
        platform: str | None = None,
    ) -> dict[str, str]:
        """Constrói o ambiente explícito sem alterar o processo do orquestrador."""
        environment = dict(os.environ)
        command = Path(arguments[0]).stem.casefold()
        uv_run = command == "uv" and "run" in arguments[1:]
        python_gate = uv_run or command in {"python", "python3", "py", "pytest"}
        if python_gate:
            environment.pop("PYTHONPATH", None)
        if python_gate and not (uv_run and "--active" in arguments[1:]):
            environment.pop("VIRTUAL_ENV", None)

        if temporary_root is not None:
            cache = (temporary_root / "pytest-cache").as_posix()
            basetemp = (temporary_root / "pytest-temp").as_posix()
            environment["PYTEST_ADDOPTS"] = (
                f"-o cache_dir={shlex.quote(cache)} --basetemp={shlex.quote(basetemp)}"
            )
            environment["PYTEST_DEBUG_TEMPROOT"] = temporary_root.as_posix()
            environment["TMPDIR"] = str(temporary_root)
            if (platform or os.name) == "nt":
                environment["TMP"] = str(temporary_root)
                environment["TEMP"] = str(temporary_root)
        return environment

    @staticmethod
    def _is_pytest_gate(arguments: Sequence[str]) -> bool:
        """Reconhece somente invocações explícitas do pytest no comando do projeto."""
        stems = [Path(argument).stem.casefold() for argument in arguments]
        if stems[0] == "pytest":
            return True
        if stems[0] in {"python", "python3", "py"}:
            return any(
                argument == "-m" and stems[index + 1] == "pytest"
                for index, argument in enumerate(arguments[:-1])
            )
        if stems[0] == "uv" and "run" in arguments[1:]:
            run_index = arguments.index("run")
            return "pytest" in stems[run_index + 1:]
        return False

    @staticmethod
    def _is_local_infrastructure_failure(
        diagnostic: str, temporary_root: Path | None
    ) -> bool:
        """Classifica conservadoramente falha de acesso ao temporário controlado."""
        if temporary_root is None:
            return False
        normalized = diagnostic.casefold().replace("\\", "/")
        root = temporary_root.as_posix().casefold()
        denied = re.search(
            r"access (?:is )?denied|permission denied|acesso negado|winerror 5",
            normalized,
        )
        return bool(denied and root in normalized)

    @staticmethod
    def _summarize(diagnostic: str) -> str:
        was_truncated = len(diagnostic) > MAX_GATE_DIAGNOSTIC_CHARACTERS
        diagnostic = sanitize_diagnostic_text(diagnostic) or ""
        if not was_truncated and len(diagnostic) <= MAX_GATE_DIAGNOSTIC_CHARACTERS:
            return diagnostic
        suffix = "… [saída truncada]"
        return diagnostic[: MAX_GATE_DIAGNOSTIC_CHARACTERS - len(suffix)] + suffix
