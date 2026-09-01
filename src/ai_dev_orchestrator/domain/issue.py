"""Modelo interno de uma issue."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    """Representa uma issue do repositório configurado."""

    number: int
    title: str
    body: str
    state: str
    url: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
