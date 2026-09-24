"""Adapter headless e estruturado do Antigravity CLI."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_dev_orchestrator.domain.provider import (
    ProviderFailure, ProviderFailureKind, classify_provider_text, FAILURE_MESSAGES,
    FAILURE_PRECEDENCE, reliable_retry_at, classify_process_failure,
    sanitized_diagnostic_context, sanitized_protocol_diagnostic,
)
from ai_dev_orchestrator.infrastructure.process import CommandRunner, OutputPolicy
from ai_dev_orchestrator.infrastructure.heartbeat import heartbeat


class AntigravityError(ProviderFailure):
    """Falha controlada na invocação headless do provider."""

    def __init__(self, message: str, classification=ProviderFailureKind.PROTOCOL_ERROR,
                 returncode=None, source="protocolo", diagnostic_context=None) -> None:
        super().__init__("gemini", classification, message, datetime.now(timezone.utc),
                         returncode=returncode, diagnostic_source=source,
                         diagnostic_context=sanitized_diagnostic_context(diagnostic_context))


def _protocol_error(kind: ProviderFailureKind, code: str, message: str,
                    returncode: int | None = None) -> AntigravityError:
    return AntigravityError(message, kind, returncode,
                           diagnostic_context=sanitized_protocol_diagnostic(code))


def _cli_schema_incompatible(detail: str) -> bool:
    """Reconhece rejeição explícita do contrato; nunca persiste o texto da CLI."""
    return bool(re.search(
        r"(?i)(?:unknown|unrecognized|unsupported|invalid) (?:option|argument|flag)"
        r"[^\r\n]*(?:--json-schema|--output-format|--input-format|--sandbox|--disable-slash-commands|--print-timeout)"
        r"|(?:invalid|unsupported|incompatible) (?:json[ -])?schema"
        r"|(?:json[ -])?schema (?:is )?(?:invalid|unsupported|incompatible)",
        detail,
    ))


class AntigravityAdapter:
    """Cada chamada inicia um processo novo, com prompt exclusivamente no stdin."""

    def __init__(self, timeout_seconds: float, runner: CommandRunner | None = None,
                 model: str = "default", executable: str = "agy",
                 progress: Callable[[str], None] | None = None,
                 heartbeat_seconds: float = 300) -> None:
        self.timeout_seconds = timeout_seconds
        self.runner = runner or CommandRunner(timeout=timeout_seconds)
        self.model = model
        self.executable = executable
        self.progress = progress
        self.heartbeat_seconds = heartbeat_seconds

    def check_available(self) -> str:
        """Mesmo preflight local no doctor e antes de cada chamada headless."""
        outputs = []
        for flag in ("--version", "--help"):
            result = self.runner.run([self.executable, flag])
            if result.error:
                self._process_failure(result)
            if not result.succeeded:
                kind = classify_provider_text("\n".join((result.stderr, result.stdout)))
                if kind is not ProviderFailureKind.UNKNOWN:
                    raise ProviderFailure("gemini", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                                          returncode=result.returncode, diagnostic_source="CLI")
                raise _protocol_error(
                    ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE, "CLI_PREFLIGHT_FAILED",
                    f"Antigravity {flag} retornou código {result.returncode}; saída omitida",
                    result.returncode,
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
            raise _protocol_error(
                ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE, "CLI_CAPABILITY_MISSING",
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
        with heartbeat("Review Gemini", self.progress, self.heartbeat_seconds):
            result = self.runner.run(
                arguments,
                cwd=cwd,
                input_text=prompt,
                stdout_policy=OutputPolicy.UTF8_STRICT,
            )
        if result.error:
            # Erros locais do runner (timeout, resolução, UTF-8) não são um
            # diagnóstico remoto do provider e devem permitir nova retomada.
            self._process_failure(result)
        if not result.succeeded:
            detail = "\n".join((result.stderr, result.stdout))
            kind = classify_provider_text(detail)
            if kind is not ProviderFailureKind.UNKNOWN:
                raise ProviderFailure("gemini", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                                      returncode=result.returncode, diagnostic_source="CLI")
            if _cli_schema_incompatible(detail):
                raise _protocol_error(
                    ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE, "CLI_SCHEMA_INCOMPATIBLE",
                    FAILURE_MESSAGES[ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE], result.returncode,
                )
            raise AntigravityError(
                f"Antigravity retornou código {result.returncode}; saída omitida",
                ProviderFailureKind.UNKNOWN, result.returncode,
            )
        def malformed(code, message):
            return _protocol_error(ProviderFailureKind.PROTOCOL_MALFORMED_RESPONSE,
                                   code, message, result.returncode)

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise malformed("JSON_DUPLICATE_KEY", "Antigravity retornou JSON com chave duplicada")
                value[key] = item
            return value

        def reject_constant(_value):
            raise malformed("JSON_NON_FINITE_NUMBER", "Antigravity retornou número incompatível com JSON")

        if len(result.stdout) > 1_048_576:
            raise malformed("JSON_OVERSIZED", "Envelope do Antigravity excede o limite de 1 MiB")
        try:
            envelope = json.loads(result.stdout, object_pairs_hook=unique_object,
                                  parse_constant=reject_constant)
        except (ValueError, RecursionError) as error:
            raise malformed("ENVELOPE_INVALID_JSON", "Antigravity retornou envelope JSON inválido") from error
        if isinstance(envelope, dict) and envelope.get("status") != "SUCCESS":
            failure = envelope.get("error")
            if isinstance(failure, str):
                kind = classify_provider_text(failure)
                if kind is not ProviderFailureKind.UNKNOWN:
                    raise ProviderFailure(
                        "gemini", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                        returncode=result.returncode, diagnostic_source="JSON"
                    )
                if _cli_schema_incompatible(failure):
                    raise _protocol_error(
                        ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE, "CLI_SCHEMA_INCOMPATIBLE",
                        FAILURE_MESSAGES[ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE], result.returncode,
                    )
            if isinstance(failure, dict):
                mapping = {"RATE_LIMIT": ProviderFailureKind.TRANSIENT_RATE_LIMIT, "QUOTA_EXCEEDED": ProviderFailureKind.TERMINAL_QUOTA, "AUTH_ERROR": ProviderFailureKind.AUTH_ERROR, "NETWORK_ERROR": ProviderFailureKind.NETWORK_ERROR, "MODEL_UNAVAILABLE": ProviderFailureKind.MODEL_UNAVAILABLE}
                kind = mapping.get(str(failure.get("code", "")).upper(), ProviderFailureKind.UNKNOWN)
                textual = classify_provider_text(str(failure.get("message", "")))
                kind = next(k for k in FAILURE_PRECEDENCE if k in {kind, textual})
                if kind is ProviderFailureKind.UNKNOWN and (
                    str(failure.get("code", "")).upper() in {"INVALID_SCHEMA", "UNSUPPORTED_SCHEMA", "SCHEMA_ERROR", "CLI_INCOMPATIBLE"}
                    or _cli_schema_incompatible(str(failure.get("message", "")))
                ):
                    raise _protocol_error(
                        ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE, "CLI_SCHEMA_INCOMPATIBLE",
                        FAILURE_MESSAGES[ProviderFailureKind.PROTOCOL_CLI_INCOMPATIBLE], result.returncode,
                    )
                retry_at = reliable_retry_at(failure.get("retry_at"))
                raise ProviderFailure("gemini", kind, FAILURE_MESSAGES[kind], datetime.now(timezone.utc), retry_at,
                                      returncode=result.returncode, diagnostic_source="JSON")
        if not isinstance(envelope, dict):
            raise malformed("ENVELOPE_NOT_OBJECT", "Envelope do Antigravity deve ser objeto")
        if envelope.get("status") != "SUCCESS":
            if envelope.get("status") == "ERROR":
                raise _protocol_error(
                    ProviderFailureKind.PROTOCOL_SEMANTIC_INVALID, "ENVELOPE_NON_SUCCESS",
                    "Antigravity não retornou status SUCCESS", result.returncode,
                )
            raise malformed("ENVELOPE_INVALID_STATUS", "Antigravity não retornou status SUCCESS")
        if envelope.get("error"):
            raise _protocol_error(
                ProviderFailureKind.PROTOCOL_SEMANTIC_INVALID, "SUCCESS_WITH_ERROR",
                "Antigravity retornou SUCCESS com erro; saída omitida", result.returncode,
            )
        if "denied_actions" in envelope and envelope["denied_actions"] != []:
            # A CLI pode encerrar com SUCCESS após negar comandos em headless.
            # Mesmo um objeto estruturado não prova que a revisão foi concluída.
            raise _protocol_error(
                ProviderFailureKind.PROTOCOL_SEMANTIC_INVALID, "DENIED_ACTIONS",
                "Antigravity retornou denied_actions: revisão incompleta por "
                "bloqueio de permissões. O reviewer headless deve analisar o dossier "
                "sem executar comandos; nenhuma aprovação foi registrada", result.returncode,
            )
        structured_output = envelope.get("structured_output")
        if "structured_output" not in envelope:
            raise malformed(
                "SUCCESS_WITHOUT_STRUCTURED_OUTPUT",
                "Falha do contrato estruturado do reviewer: Antigravity retornou SUCCESS sem structured_output",
            )
        if not isinstance(structured_output, dict):
            raise _protocol_error(
                ProviderFailureKind.PROTOCOL_SCHEMA_MISMATCH, "STRUCTURED_OUTPUT_NOT_OBJECT",
                "Falha do contrato estruturado do reviewer: Antigravity retornou "
                "SUCCESS sem structured_output compatível", result.returncode,
            )
        return json.dumps(structured_output)

    @staticmethod
    def _process_failure(result) -> None:
        kind = classify_process_failure(result.failure_kind)
        message = FAILURE_MESSAGES[kind]
        if kind == ProviderFailureKind.EXECUTABLE_MISSING:
            message += "; configure review.executable (ORCH_REVIEW__EXECUTABLE) ou ajuste o PATH"
        raise AntigravityError(message, kind, result.returncode, "processo")
