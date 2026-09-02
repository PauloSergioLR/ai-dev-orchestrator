"""Adapter headless e estruturado do Antigravity CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_dev_orchestrator.infrastructure.process import CommandRunner


class AntigravityError(Exception):
    """Falha controlada na invocação headless do provider."""


class AntigravityAdapter:
    """Cada chamada inicia um processo novo, com prompt exclusivamente no stdin."""

    def __init__(self, timeout_seconds: float, runner: CommandRunner | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.runner = runner or CommandRunner(timeout=timeout_seconds)

    def invoke(self, prompt: str, cwd: str | Path, schema: dict[str, Any]) -> str:
        arguments = [
            "agy", "--input-format", "text", "--dangerously-skip-permissions", "--print-timeout",
            f"{int(self.timeout_seconds)}s", "--output-format", "json", "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        result = self.runner.run(arguments, cwd=cwd, input_text=prompt)
        if result.error:
            raise AntigravityError(f"Falha ao executar Antigravity: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            raise AntigravityError(f"Antigravity retornou código {result.returncode}: {detail}")
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AntigravityError("Antigravity retornou envelope JSON inválido") from error
        if not isinstance(envelope, dict) or envelope.get("status") != "SUCCESS":
            raise AntigravityError("Antigravity não retornou status SUCCESS")
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            raise AntigravityError("Antigravity não retornou structured_output compatível")
        return json.dumps(structured_output)
