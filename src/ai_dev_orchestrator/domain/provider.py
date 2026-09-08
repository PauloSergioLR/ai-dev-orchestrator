"""Classificação segura de falhas reportadas por providers locais."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re


class ProviderFailureKind(StrEnum):
    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    TERMINAL_QUOTA = "TERMINAL_QUOTA"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
    LOCAL_TRANSIENT = "LOCAL_TRANSIENT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    MALFORMED_JSON = "MALFORMED_JSON"
    ENCODING_ERROR = "ENCODING_ERROR"
    PROCESS_CLEANUP_ERROR = "PROCESS_CLEANUP_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderFailure(Exception):
    provider: str
    classification: ProviderFailureKind
    message: str
    observed_at: datetime
    retry_at: datetime | None = None
    session_id: str | None = None
    returncode: int | None = None
    diagnostic_source: str = "provider"

    def __str__(self) -> str:
        return (f"{self.provider}: {self.classification.value}: {self.message} "
                f"(exit={self.returncode}, fonte={self.diagnostic_source})")


class FailureDisposition(StrEnum):
    WAIT_RESET = "WAIT_RESET"
    RETRY = "RETRY"
    INTERVENTION = "INTERVENTION"


# Matriz exaustiva: limites pertencem à conta; falhas técnicas têm orçamento próprio.
FAILURE_POLICY = {
    ProviderFailureKind.TERMINAL_QUOTA: FailureDisposition.WAIT_RESET,
    ProviderFailureKind.TRANSIENT_RATE_LIMIT: FailureDisposition.WAIT_RESET,
    ProviderFailureKind.NETWORK_ERROR: FailureDisposition.RETRY,
    ProviderFailureKind.TIMEOUT: FailureDisposition.RETRY,
    ProviderFailureKind.LOCAL_TRANSIENT: FailureDisposition.RETRY,
    ProviderFailureKind.EXECUTABLE_MISSING: FailureDisposition.INTERVENTION,
    ProviderFailureKind.AUTH_ERROR: FailureDisposition.INTERVENTION,
    ProviderFailureKind.MODEL_UNAVAILABLE: FailureDisposition.INTERVENTION,
    ProviderFailureKind.UNKNOWN: FailureDisposition.INTERVENTION,
    ProviderFailureKind.PROTOCOL_ERROR: FailureDisposition.INTERVENTION,
    ProviderFailureKind.MALFORMED_JSON: FailureDisposition.INTERVENTION,
    ProviderFailureKind.ENCODING_ERROR: FailureDisposition.INTERVENTION,
    ProviderFailureKind.PROCESS_CLEANUP_ERROR: FailureDisposition.INTERVENTION,
}

FAILURE_PRECEDENCE = (
    ProviderFailureKind.TERMINAL_QUOTA,
    ProviderFailureKind.AUTH_ERROR,
    ProviderFailureKind.MODEL_UNAVAILABLE,
    ProviderFailureKind.TRANSIENT_RATE_LIMIT,
    ProviderFailureKind.NETWORK_ERROR,
    ProviderFailureKind.TIMEOUT,
    ProviderFailureKind.UNKNOWN,
)

FAILURE_MESSAGES = {
    ProviderFailureKind.TERMINAL_QUOTA: "Limite de uso/quota esgotado",
    ProviderFailureKind.TRANSIENT_RATE_LIMIT: "Limite temporário de requisições",
    ProviderFailureKind.NETWORK_ERROR: "Falha de transporte reportada pela CLI",
    ProviderFailureKind.TIMEOUT: "Comando excedeu o timeout",
    ProviderFailureKind.LOCAL_TRANSIENT: "Falha local ao iniciar processo",
    ProviderFailureKind.EXECUTABLE_MISSING: "Executável não encontrado",
    ProviderFailureKind.AUTH_ERROR: "Autenticação recusada; intervenção necessária",
    ProviderFailureKind.MODEL_UNAVAILABLE: "Modelo indisponível; intervenção necessária",
    ProviderFailureKind.UNKNOWN: "Falha desconhecida; saída omitida",
    ProviderFailureKind.PROTOCOL_ERROR: "Contrato do protocolo inválido",
    ProviderFailureKind.MALFORMED_JSON: "JSON/JSONL inválido",
    ProviderFailureKind.ENCODING_ERROR: "Encoding UTF-8 inválido",
    ProviderFailureKind.PROCESS_CLEANUP_ERROR: "Timeout sem prova de encerramento da árvore de processos; retry bloqueado",
}


def classify_process_failure(kind: str | None) -> ProviderFailureKind:
    """Tradução única dos diagnósticos locais, independente de mensagens do SO."""
    return {
        "TIMEOUT": ProviderFailureKind.TIMEOUT,
        "EXECUTABLE_MISSING": ProviderFailureKind.EXECUTABLE_MISSING,
        "OS_ERROR": ProviderFailureKind.LOCAL_TRANSIENT,
        "ENCODING_ERROR": ProviderFailureKind.ENCODING_ERROR,
        "PROCESS_CLEANUP_ERROR": ProviderFailureKind.PROCESS_CLEANUP_ERROR,
    }.get(kind, ProviderFailureKind.UNKNOWN)


def reliable_retry_at(value: object) -> datetime | None:
    """Aceita somente data completa e fuso explícito; não infere dia ou timezone."""
    if not isinstance(value, str):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def textual_retry_at(message: str) -> datetime | None:
    values = re.findall(r"(?i)try again (?:at|after) (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))(?=$|[\s.,;])", message)
    parsed = {value for raw in values if (value := reliable_retry_at(raw)) is not None}
    return next(iter(parsed)) if len(parsed) == 1 else None


def classify_provider_text(text: str) -> ProviderFailureKind:
    """Fallback conservador quando a CLI não entrega erro estruturado."""
    normalized = " ".join(text.casefold().split())
    patterns = (
        (
            ProviderFailureKind.TERMINAL_QUOTA,
            r"\b(quota exceeded|usage limit|insufficient quota)\b",
        ),
        (
            ProviderFailureKind.AUTH_ERROR,
            r"\b(unauthorized|forbidden|invalid api key|invalid credentials|authentication failed|login required)\b",
        ),
        (
            ProviderFailureKind.MODEL_UNAVAILABLE,
            r"\b(model (not found|unavailable|unsupported)|unknown model)\b",
        ),
        (
            ProviderFailureKind.NETWORK_ERROR,
            r"\b(connection (refused|reset)|network (unreachable|error)|dns)\b",
        ),
        (ProviderFailureKind.TIMEOUT, r"\b(timed out|timeout)\b"),
        (
            ProviderFailureKind.TRANSIENT_RATE_LIMIT,
            r"\b(rate.?limit|too many requests|temporarily exhausted|try again after)\b",
        ),
    )
    found = {kind for kind, pattern in patterns if re.search(pattern, normalized)}
    return next((kind for kind in FAILURE_PRECEDENCE if kind in found), ProviderFailureKind.UNKNOWN)
