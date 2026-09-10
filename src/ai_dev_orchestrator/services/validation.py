"""Validações locais independentes executadas após o Codex."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Protocol, Sequence

from ai_dev_orchestrator.domain.project_contract import CommandPlan, ProjectContract, RiskClass
from ai_dev_orchestrator.domain.provider import ProviderFailure, FAILURE_MESSAGES, classify_process_failure
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.infrastructure.database import sanitize_diagnostic_text

MAX_GATE_DIAGNOSTIC_CHARACTERS = 500


class LocalValidationError(Exception):
    """Indica que um gate local obrigatório falhou."""

    def __init__(self, message: str, *, result: "GateResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


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

    def __init__(self, runner: ProcessRunner | None = None, plans: Sequence[CommandPlan] | None = None) -> None:
        self.runner = runner
        self.plans = tuple(plans) if plans is not None else None

    def validate(self, worktree: str | Path, contract: ProjectContract | None = None) -> tuple[GateResult, ...]:
        configured = self.plans or (() if contract is None else (*contract.bootstrap, *contract.gates))
        plans = tuple(plan for plan in configured if plan.required)
        if not plans:
            raise LocalValidationError("Contrato não contém gates locais executáveis")
        root = Path(worktree).resolve()
        results: list[GateResult] = []
        for plan in plans:
            if plan.risk_class is not RiskClass.SAFE_LOCAL:
                raise LocalValidationError(f"Gate '{plan.name}' não é uma operação local segura")
            cwd = (root / plan.cwd).resolve()
            if cwd != root and root not in cwd.parents:
                raise LocalValidationError(f"cwd do gate '{plan.name}' escapa do worktree")
            if not cwd.is_dir():
                raise LocalValidationError(f"cwd do gate '{plan.name}' não existe: {plan.cwd}")
            runner = self.runner or CommandRunner(timeout=plan.timeout_seconds)
            started = monotonic()
            result = runner.run(plan.argv, cwd=cwd)
            duration = monotonic() - started
            if result.failure_kind is not None:
                kind = classify_process_failure(result.failure_kind)
                raise LocalProcessFailure(
                    "local", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                    returncode=result.returncode, diagnostic_source="processo",
                )
            diagnostic = self._summarize(result.error or result.stderr.strip() or result.stdout.strip())
            gate = GateResult(plan.name, plan.argv, result.succeeded, result.returncode, diagnostic, duration, plan.capability)
            results.append(gate)
            if not gate.succeeded:
                detail = f": {diagnostic}" if diagnostic else ""
                raise LocalValidationError(f"Gate local '{plan.name}' falhou{detail}", result=gate)
        return tuple(results)

    @staticmethod
    def _summarize(diagnostic: str) -> str:
        was_truncated = len(diagnostic) > MAX_GATE_DIAGNOSTIC_CHARACTERS
        diagnostic = sanitize_diagnostic_text(diagnostic) or ""
        if not was_truncated and len(diagnostic) <= MAX_GATE_DIAGNOSTIC_CHARACTERS:
            return diagnostic
        suffix = "… [saída truncada]"
        return diagnostic[: MAX_GATE_DIAGNOSTIC_CHARACTERS - len(suffix)] + suffix
