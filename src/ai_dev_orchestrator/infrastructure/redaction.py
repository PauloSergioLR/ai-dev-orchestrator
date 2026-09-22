"""Redação compartilhada de segredos nas fronteiras de diagnóstico e entrega."""

from __future__ import annotations

import os
import re


def redact_secrets(value: str) -> str:
    """Preserva o texto útil, removendo credenciais conhecidas e formatos usuais."""
    secrets = {
        secret for name, secret in os.environ.items()
        if secret and any(marker in name.upper() for marker in (
            "TOKEN", "PASSWORD", "SECRET", "WEBHOOK", "API_KEY", "APIKEY",
            "ACCESS_KEY", "PRIVATE_KEY",
        ))
    }
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[redigido]")
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redigido]@", value)
    value = re.sub(
        r"(?i)(authorization[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:bearer\s+|basic\s+)?[^\s,;}]+)",
        r"\1[redigido]", value,
    )
    value = re.sub(
        r"(?i)((?:[\w-]*(?:token|password|secret|webhook|api[_-]?key|access[_-]?key|private[_-]?key))[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redigido]", value,
    )
    return value


def sanitize_diagnostic_text(value: str | None) -> str | None:
    """Produz um diagnóstico curto que não inclui saída bruta nem credenciais."""
    return redact_secrets(value).replace("\r", " ").replace("\n", " ")[:1000] if value is not None else None


def sanitize_diagnostic(value: str) -> str:
    return sanitize_diagnostic_text(value) or ""


class RedactedError(Exception):
    """Exceção de adapter cujo diagnóstico público já foi sanitizado."""

    def __init__(self, message: str) -> None:
        super().__init__(sanitize_diagnostic(message))
