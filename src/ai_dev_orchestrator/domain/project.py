"""Modelos internos e regras relacionadas a itens do GitHub Project."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectItem:
    """Representa um item do Project sem expor o payload bruto do GitHub CLI."""

    id: str
    content_type: str
    issue_number: int | None
    title: str | None
    url: str | None
    repository: str | None
    status: str | None
    priority: str | None
    size: str | None
    risk: str | None
    agent: str | None

    @property
    def is_issue(self) -> bool:
        """Indica se o item representa uma Issue do GitHub."""
        return self.content_type == "Issue"


@dataclass(frozen=True)
class ProjectMetadata:
    """Identificação imutável de um GitHub Project configurado."""

    id: str


@dataclass(frozen=True)
class ProjectStatusOption:
    """Opção disponível no campo de seleção única de status."""

    id: str
    name: str


@dataclass(frozen=True)
class ProjectStatusField:
    """Campo Status e as opções que podem ser gravadas nele."""

    id: str
    name: str
    options: tuple[ProjectStatusOption, ...]


def is_eligible_for_execution(
    item: ProjectItem, repository: str, ready_status: str
) -> bool:
    """Aplica a regra de elegibilidade com comparação exata de texto."""
    return (
        item.is_issue
        and item.repository == repository
        and item.status == ready_status
    )
