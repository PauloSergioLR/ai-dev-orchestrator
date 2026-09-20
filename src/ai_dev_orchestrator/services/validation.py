"""Validações locais independentes executadas após o Codex."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timezone
from enum import StrEnum
import os
import json
from pathlib import Path
import re
from time import monotonic
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import Protocol, Sequence

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
    LOCAL_ENVIRONMENT_ERROR = "LOCAL_ENVIRONMENT_ERROR"
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
    def run(self, arguments: Sequence[str], cwd: str | Path | None = None) -> CommandResult: ...


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
            if self.runner is None or isinstance(self.runner, CommandRunner):
                try:
                    temporary = TemporaryDirectory(prefix="orch-python-gate-") if self._is_python_gate(plan.argv) else nullcontext(None)
                    with temporary as gate_temp:
                        result = (self.runner or CommandRunner(timeout=plan.timeout_seconds)).run(
                            plan.argv,
                            cwd=cwd,
                            environment=self._gate_environment(plan.argv, gate_temp, root),
                        )
                except OSError as error:
                    raise LocalValidationError(
                        "Não foi possível preparar ou liberar o ambiente temporário do gate",
                        kind=LocalFailureKind.LOCAL_ENVIRONMENT_ERROR, correctable=False,
                    ) from error
            else:
                result = self.runner.run(plan.argv, cwd=cwd)
            duration = monotonic() - started
            if result.failure_kind is not None:
                kind = classify_process_failure(result.failure_kind)
                raise LocalProcessFailure(
                    "local", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                    returncode=result.returncode, diagnostic_source="processo",
                )
            # Classificar antes de truncar: traceback ambiental pode vir após um log longo.
            evidence = result.error or "\n".join((result.stderr, result.stdout))
            diagnostic = self._summarize(result.error or result.stderr.strip() or result.stdout.strip())
            gate = GateResult(plan.name, plan.argv, result.succeeded, result.returncode, diagnostic, duration, plan.capability)
            results.append(gate)
            if self.progress:
                outcome = "aprovado" if gate.succeeded else "reprovado"
                self.progress(f"Gate local {outcome}: {plan.name}")
            if not gate.succeeded:
                detail = f": {diagnostic}" if diagnostic else ""
                missing_environment = bool(re.search(
                    r"(?i)(environment variable|vari[aá]vel de ambiente|not set|undefined variable|missing env)",
                    evidence,
                ))
                environment_failure = bool(
                    re.search(r"(?i)(PermissionError|WinError\s*(?:5|32)|Permission denied|acesso negado)", evidence)
                    and re.search(r"(?i)(\.pytest_cache|pytest-of-|orch-python-gate-|[\\/]temp[\\/]|[\\/]tmp[\\/]|uv[\\/]cache|\.venv)", evidence)
                )
                kind = (
                    LocalFailureKind.LOCAL_ENVIRONMENT_ERROR
                    if environment_failure
                    else
                    LocalFailureKind.MISSING_REQUIRED_ENVIRONMENT
                    if missing_environment
                    else LocalFailureKind.PROJECT_BUILD_FAILURE
                    if "build" in plan.capability.casefold()
                    else LocalFailureKind.PROJECT_TEST_FAILURE
                )
                raise LocalValidationError(
                    f"Gate local '{plan.name}' falhou{detail}",
                    result=gate,
                    kind=kind,
                    correctable=not (missing_environment or environment_failure),
                )
        return tuple(results)

    @staticmethod
    def _is_python_gate(arguments: Sequence[str]) -> bool:
        command = Path(arguments[0]).stem.casefold()
        return command in {
            "uv", "uvx", "pytest", "ruff", "tox", "nox", "poetry", "pdm",
            "hatch", "coverage", "mypy", "pip", "pip3",
        } or re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", command) is not None

    @staticmethod
    def _gate_environment(arguments: Sequence[str], temporary_directory: str | None = None,
                          worktree: Path | None = None) -> dict[str, str] | None:
        """Isola opções, import paths e temporários apenas nos gates Python."""
        if not LocalValidationService._is_python_gate(arguments):
            return None
        environment = dict(os.environ)
        for name in (
            "PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX", "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS", "PYTEST_DEBUG_TEMPROOT", "UV_PROJECT_ENVIRONMENT",
            "UV_CACHE_DIR", "PIP_CACHE_DIR", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR",
            "TMP", "TEMP", "TMPDIR",
        ):
            environment.pop(name, None)
        if "--active" not in arguments[1:]:
            active = environment.pop("VIRTUAL_ENV", None)
            if active:
                active_root = Path(active).resolve()
                environment["PATH"] = os.pathsep.join(
                    value for value in environment.get("PATH", os.defpath).split(os.pathsep)
                    if Path(value).resolve() not in {active_root, active_root / "Scripts", active_root / "bin"}
                )
        if temporary_directory is not None:
            environment.update({name: temporary_directory for name in ("TMP", "TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT")})
            if any(Path(argument).stem.casefold() == "pytest" for argument in arguments):
                cache = (Path(temporary_directory) / "pytest-cache").as_posix()
                environment["PYTEST_ADDOPTS"] = "-o " + json.dumps("cache_dir=" + cache, ensure_ascii=False)
        if worktree is not None and "--active" not in arguments[1:]:
            scripts = worktree / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            if scripts.is_dir() and scripts.resolve().is_relative_to(worktree.resolve()):
                environment["PATH"] = str(scripts) + os.pathsep + environment.get("PATH", os.defpath)
        return environment if environment != dict(os.environ) else None

    @staticmethod
    def _summarize(diagnostic: str) -> str:
        was_truncated = len(diagnostic) > MAX_GATE_DIAGNOSTIC_CHARACTERS
        diagnostic = sanitize_diagnostic_text(diagnostic) or ""
        if not was_truncated and len(diagnostic) <= MAX_GATE_DIAGNOSTIC_CHARACTERS:
            return diagnostic
        suffix = "… [saída truncada]"
        return diagnostic[: MAX_GATE_DIAGNOSTIC_CHARACTERS - len(suffix)] + suffix
