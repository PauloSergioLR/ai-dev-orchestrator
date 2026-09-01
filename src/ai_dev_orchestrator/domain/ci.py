"""Modelos imutáveis da consulta e decisão da CI de um Pull Request."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CiStatus(StrEnum):
    """Estado agregado, deliberadamente pequeno, do gate de CI."""

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True)
class StatusCheck:
    """Um check normalizado da resposta estruturada do GitHub."""

    name: str
    status: str
    conclusion: str | None
    details_url: str | None = None


@dataclass(frozen=True)
class PullRequestCiSnapshot:
    """HEAD e checks observados em uma única consulta do Pull Request."""

    head_sha: str
    checks: tuple[StatusCheck, ...]


@dataclass(frozen=True)
class CiResult:
    """Resultado estruturado do gate, inclusive quando ele falha."""

    expected_head_sha: str
    checks: tuple[StatusCheck, ...]
    status: CiStatus
