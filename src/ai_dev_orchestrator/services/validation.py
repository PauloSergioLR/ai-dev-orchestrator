"""Validações locais independentes executadas após o Codex."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


LOCAL_GATE_TIMEOUT_SECONDS = 120
MAX_GATE_DIAGNOSTIC_CHARACTERS = 500


class LocalValidationError(Exception):
    """Indica que um gate local obrigatório falhou."""


class ProcessRunner(Protocol):
    def run(self, arguments: Sequence[str], cwd: str | Path | None = None) -> CommandResult: ...


@dataclass(frozen=True)
class GateResult:
    name: str
    command: tuple[str, ...]
    succeeded: bool
    returncode: int | None
    diagnostic: str


class LocalValidationService:
    """Executa os gates obrigatórios no worktree, em ordem e fail-fast."""

    gates = (
        ("ruff", ("uv", "run", "ruff", "check", ".")),
        ("pytest", ("uv", "run", "pytest")),
        ("diff_check", ("git", "diff", "--check")),
    )

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or CommandRunner(timeout=LOCAL_GATE_TIMEOUT_SECONDS)

    def validate(self, worktree: str | Path) -> tuple[GateResult, ...]:
        results: list[GateResult] = []
        for name, command in self.gates:
            result = self.runner.run(command, cwd=worktree)
            diagnostic = self._summarize(result.error or result.stderr.strip() or result.stdout.strip())
            gate = GateResult(name, command, result.succeeded, result.returncode, diagnostic)
            results.append(gate)
            if not gate.succeeded:
                detail = f": {diagnostic}" if diagnostic else ""
                raise LocalValidationError(f"Gate local '{name}' falhou{detail}")
        return tuple(results)

    @staticmethod
    def _summarize(diagnostic: str) -> str:
        if len(diagnostic) <= MAX_GATE_DIAGNOSTIC_CHARACTERS:
            return diagnostic
        return f"{diagnostic[:MAX_GATE_DIAGNOSTIC_CHARACTERS]}… [saída truncada]"
