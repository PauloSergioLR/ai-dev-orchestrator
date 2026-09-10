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


LOGICAL_PROJECT_STATES = (
    "queue", "ready", "implementing", "waiting_ci", "ai_review",
    "human_required", "completed",
)


def infer_status_mapping(options: tuple[ProjectStatusOption, ...]) -> dict[str, str] | None:
    """Propõe mapeamento semântico; devolve None diante de ambiguidade real."""
    import unicodedata

    def normalized(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold())
        return "".join(character for character in value if not unicodedata.combining(character))

    names = [option.name for option in options]
    if not names:
        return None
    tokens = {name: normalized(name) for name in names}

    def choose(words: tuple[str, ...]) -> str | None:
        matches = [name for name, text in tokens.items() if any(word in text for word in words)]
        return matches[0] if len(matches) == 1 else None

    todo = choose(("todo", "backlog", "ready", "pronto", "fila"))
    doing = choose(("progress", "andamento", "doing", "review", "revisao"))
    done = choose(("done", "finalizado", "concluido", "complete"))
    if len(names) == 3 and todo and doing and done:
        return {
            "queue": todo, "ready": todo, "implementing": doing,
            "waiting_ci": doing, "ai_review": doing, "human_required": doing,
            "completed": done,
        }
    mapping = {
        "queue": choose(("backlog", "todo", "fila")),
        "ready": choose(("ready", "pronto")),
        "implementing": choose(("progress", "andamento", "doing")),
        "waiting_ci": choose(("ci", "progress", "andamento")),
        "ai_review": choose(("ai review", "revisao ia", "review")),
        "human_required": choose(("human", "humano", "blocked", "bloqueado")),
        "completed": done,
    }
    return {key: value for key, value in mapping.items() if value} if all(
        mapping.get(required) for required in ("ready", "implementing", "completed")
    ) else None


def is_eligible_for_execution(
    item: ProjectItem, repository: str, ready_status: str
) -> bool:
    """Aplica a regra de elegibilidade com comparação exata de texto."""
    return (
        item.is_issue
        and item.repository == repository
        and item.status == ready_status
    )
