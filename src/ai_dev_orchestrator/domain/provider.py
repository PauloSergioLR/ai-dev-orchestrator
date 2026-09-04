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
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderFailure(Exception):
    provider: str
    classification: ProviderFailureKind
    message: str
    observed_at: datetime
    retry_at: datetime | None = None
    session_id: str | None = None

    def __str__(self) -> str:
        return f"{self.provider}: {self.classification.value}: {self.message}"


def classify_provider_text(text: str) -> ProviderFailureKind:
    """Fallback conservador quando a CLI não entrega erro estruturado."""
    normalized = " ".join(text.casefold().split())
    patterns = (
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
            r"\b(connection (refused|reset)|network (unreachable|error)|dns|timed out|timeout)\b",
        ),
        (
            ProviderFailureKind.TRANSIENT_RATE_LIMIT,
            r"\b(rate.?limit|too many requests|temporarily exhausted|try again after)\b",
        ),
        (
            ProviderFailureKind.TERMINAL_QUOTA,
            r"\b(quota exceeded|usage limit|insufficient quota)\b",
        ),
    )
    for kind, pattern in patterns:
        if re.search(pattern, normalized):
            return kind
    return ProviderFailureKind.UNKNOWN
