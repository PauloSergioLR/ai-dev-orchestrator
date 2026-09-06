"""Adapter headless e estruturado do Antigravity CLI."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind, classify_provider_text
from ai_dev_orchestrator.infrastructure.process import CommandRunner


class AntigravityError(Exception):
    """Falha controlada na invocação headless do provider."""


class AntigravityAdapter:
    """Cada chamada inicia um processo novo, com prompt exclusivamente no stdin."""

    def __init__(self, timeout_seconds: float, runner: CommandRunner | None = None, model: str = "default", executable: str = "agy") -> None:
        self.timeout_seconds = timeout_seconds
        self.runner = runner or CommandRunner(timeout=timeout_seconds)
        self.model = model
        self.executable = executable

    def check_available(self) -> str:
        """Mesmo preflight local no doctor e antes de cada chamada headless."""
        outputs = []
        for flag in ("--version", "--help"):
            result = self.runner.run([self.executable, flag])
            if result.error:
                raise AntigravityError(
                    f"Antigravity indisponível: {result.error}. "
                    "Configure review.executable (ORCH_REVIEW__EXECUTABLE) com o "
                    "caminho da CLI ou ajuste o PATH do processo."
                )
            if not result.succeeded:
                raise AntigravityError(
                    f"Antigravity {flag} retornou código {result.returncode}; saída omitida"
                )
            outputs.append("\n".join((result.stdout, result.stderr)))
        required = {
            "--input-format", "--sandbox", "--disable-slash-commands",
            "--print-timeout", "--output-format", "--json-schema",
        }
        if self.model != "default":
            required.add("--model")
        declared = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*(?![\w-])", outputs[1]))
        missing = sorted(required - declared)
        if missing:
            raise AntigravityError(
                "CLI incompatível com review estruturado; flags ausentes: "
                + ", ".join(missing)
            )
        return outputs[0].strip() or "disponível"

    def invoke(self, prompt: str, cwd: str | Path, schema: dict[str, Any]) -> str:
        self.check_available()
        # stdin text foi validado na CLI 1.1.27; evita o limite de argv do Windows.
        arguments = [
            self.executable,
            "--input-format",
            "text",
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            f"{self.timeout_seconds:g}s",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if self.model != "default":
            arguments.extend(["--model", self.model])
        result = self.runner.run(arguments, cwd=cwd, input_text=prompt)
        if result.error:
            # Erros locais do runner (timeout, resolução, UTF-8) não são um
            # diagnóstico remoto do provider e devem permitir nova retomada.
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
            if isinstance(failure, str):
                kind = classify_provider_text(failure)
                if kind is not ProviderFailureKind.UNKNOWN:
                    raise ProviderFailure(
                        "gemini", kind, "Falha reportada pela CLI", datetime.now(timezone.utc)
                    )
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
        if envelope.get("error"):
            raise AntigravityError("Antigravity retornou SUCCESS com erro; saída omitida")
        if "denied_actions" in envelope and envelope["denied_actions"] != []:
            # A CLI pode encerrar com SUCCESS após negar comandos em headless.
            # Mesmo um objeto estruturado não prova que a revisão foi concluída.
            raise AntigravityError(
                "Antigravity retornou denied_actions: revisão incompleta por "
                "bloqueio de permissões. O reviewer headless deve analisar o dossier "
                "sem executar comandos; nenhuma aprovação foi registrada"
            )
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            raise AntigravityError(
                "Falha do contrato estruturado do reviewer: Antigravity retornou "
                "SUCCESS sem structured_output compatível"
            )
        return json.dumps(structured_output)
