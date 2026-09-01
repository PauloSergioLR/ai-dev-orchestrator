"""Adapter headless, sem estado implícito, do Antigravity CLI."""
from __future__ import annotations
from ai_dev_orchestrator.infrastructure.process import CommandRunner

class AntigravityError(Exception):
    """Falha controlada na invocação headless do provider."""

class AntigravityAdapter:
    def __init__(self, timeout_seconds: float, runner: CommandRunner | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.runner = runner or CommandRunner(timeout=timeout_seconds)
    def invoke(self, prompt: str) -> str:
        result = self.runner.run(["agy", "-p", prompt, "--dangerously-skip-permissions", "--print-timeout", str(int(self.timeout_seconds))])
        if result.error:
            raise AntigravityError(f"Falha ao executar Antigravity: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            raise AntigravityError(f"Antigravity retornou código {result.returncode}: {detail}")
        if not result.stdout.strip():
            raise AntigravityError("Antigravity retornou saída vazia")
        return result.stdout
