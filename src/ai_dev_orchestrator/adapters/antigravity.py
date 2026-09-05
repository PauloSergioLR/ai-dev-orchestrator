"""Adapter headless e estruturado do Antigravity CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind, classify_provider_text
from ai_dev_orchestrator.infrastructure.process import CommandRunner


class AntigravityError(Exception):
    """Falha controlada na invocação headless do provider."""


class AntigravityAdapter:
    """Cada chamada inicia um processo novo, com prompt exclusivamente no stdin."""

    def __init__(self, timeout_seconds: float, runner: CommandRunner | None = None, model: str = "default") -> None:
        self.timeout_seconds = timeout_seconds
        self.runner = runner or CommandRunner(timeout=timeout_seconds)
        self.model = model

    def invoke(self, prompt: str, cwd: str | Path, schema: dict[str, Any]) -> str:
        # `--mode plan` altera o contrato da execução headless e pode encerrar a
        # chamada com SUCCESS sem materializar o resultado imposto por --json-schema.
        # O sandbox mantém o reviewer contido sem trocar o modo de resposta.
        arguments = [
            "agy",
            "--input-format",
            "text",
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            f"{int(self.timeout_seconds)}s",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if self.model != "default":
            arguments.extend(["--model", self.model])
        result = self.runner.run(arguments, cwd=cwd, input_text=prompt)
        if result.error:
            kind = classify_provider_text(result.error)
            if kind is not ProviderFailureKind.UNKNOWN:
                raise ProviderFailure(
                    "gemini", kind, "Falha ao invocar a CLI", datetime.now(timezone.utc)
                )
            raise AntigravityError(f"Falha ao executar Antigravity: {result.error}")
        if not result.succeeded:
            detail = "\n".join((result.stderr, result.stdout))
            kind = classify_provider_text(detail)
            if kind is not ProviderFailureKind.UNKNOWN:
                raise ProviderFailure("gemini", kind, "Falha reportada pela CLI", datetime.now(timezone.utc))
            raise AntigravityError(
                f"Antigravity retornou código {result.returncode}; saída omitida"
            )
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AntigravityError("Antigravity retornou envelope JSON inválido") from error
        if isinstance(envelope, dict) and envelope.get("status") != "SUCCESS":
            failure = envelope.get("error")
            if isinstance(failure, dict):
                mapping = {"RATE_LIMIT": ProviderFailureKind.TRANSIENT_RATE_LIMIT, "QUOTA_EXCEEDED": ProviderFailureKind.TERMINAL_QUOTA, "AUTH_ERROR": ProviderFailureKind.AUTH_ERROR, "NETWORK_ERROR": ProviderFailureKind.NETWORK_ERROR, "MODEL_UNAVAILABLE": ProviderFailureKind.MODEL_UNAVAILABLE}
                kind = mapping.get(str(failure.get("code", "")).upper(), ProviderFailureKind.UNKNOWN)
                retry_at = None
                if isinstance(failure.get("retry_at"), str):
                    try:
                        retry_at = datetime.fromisoformat(failure["retry_at"].replace("Z", "+00:00"))
                        if retry_at.tzinfo is None:
                            retry_at = None
                    except ValueError:
                        pass
                raise ProviderFailure("gemini", kind, "Falha reportada pela CLI", datetime.now(timezone.utc), retry_at)
        if not isinstance(envelope, dict) or envelope.get("status") != "SUCCESS":
            raise AntigravityError("Antigravity não retornou status SUCCESS")
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            raise AntigravityError(
                "Falha do contrato estruturado do reviewer: Antigravity retornou "
                "SUCCESS sem structured_output compatível"
            )
        return json.dumps(structured_output)
