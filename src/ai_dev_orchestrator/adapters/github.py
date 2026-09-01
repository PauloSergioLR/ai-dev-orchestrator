"""Adapter somente de leitura para issues do GitHub via GitHub CLI."""

from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.config import GitHubConfig, OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


GITHUB_ISSUE_TIMEOUT_SECONDS = 20
GITHUB_PROJECT_TIMEOUT_SECONDS = 20


class GitHubIssueError(Exception):
    """Indica que uma issue não pôde ser carregada do GitHub."""


class GitHubProjectError(Exception):
    """Indica que os itens de um GitHub Project não puderam ser carregados."""


class ProcessRunner(Protocol):
    """Contrato mínimo do executor usado pelo adapter."""

    def run(self, arguments: Sequence[str]) -> CommandResult:
        """Executa um processo local."""


class GitHubIssueAdapter:
    """Obtém issues do repositório configurado sem alterar o GitHub."""

    def __init__(
        self,
        config: GitHubConfig | OrchestratorConfig,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config.github if isinstance(config, OrchestratorConfig) else config
        self.runner = (
            runner
            if runner is not None
            else CommandRunner(timeout=GITHUB_ISSUE_TIMEOUT_SECONDS)
        )

    def get_issue(self, number: int) -> Issue:
        """Carrega uma issue pelo número usando o GitHub CLI autenticado."""
        arguments = [
            "gh", "issue", "view", str(number), "--repo",
            f"{self.config.owner}/{self.config.repository}", "--json",
            "number,title,body,state,url,labels,assignees",
        ]
        result = self.runner.run(arguments)
        if result.error:
            raise GitHubIssueError(
                f"Não foi possível executar o GitHub CLI: {result.error}"
            )
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"GitHub CLI retornou código {result.returncode} ao ler a issue {number}"
            if detail:
                message = f"{message}: {detail}"
            raise GitHubIssueError(message)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueError("GitHub CLI retornou JSON inválido para a issue") from error

        return self._parse_issue(payload)

    @classmethod
    def _parse_issue(cls, payload: Any) -> Issue:
        """Converte o JSON do GitHub CLI para o modelo interno validado."""
        if not isinstance(payload, dict):
            raise GitHubIssueError("Resposta da issue inválida: objeto JSON esperado")
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise GitHubIssueError("Resposta da issue inválida: campo 'body' deve ser texto ou nulo")
        return Issue(
            number=cls._required_int(payload, "number"),
            title=cls._required_string(payload, "title"),
            body=body,
            state=cls._required_string(payload, "state"),
            url=cls._required_string(payload, "url"),
            labels=cls._names(payload, "labels", "name"),
            assignees=cls._names(payload, "assignees", "login"),
        )

    @staticmethod
    def _required_int(payload: dict[str, Any], field: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise GitHubIssueError(f"Resposta da issue inválida: campo '{field}' deve ser inteiro")
        return value

    @staticmethod
    def _required_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str):
            raise GitHubIssueError(f"Resposta da issue inválida: campo '{field}' deve ser texto")
        return value

    @staticmethod
    def _names(payload: dict[str, Any], field: str, name_field: str) -> tuple[str, ...]:
        values = payload.get(field)
        if not isinstance(values, list):
            raise GitHubIssueError(f"Resposta da issue inválida: campo '{field}' deve ser uma lista")
        names: list[str] = []
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get(name_field), str):
                raise GitHubIssueError(
                    f"Resposta da issue inválida: itens de '{field}' devem conter '{name_field}'"
                )
            names.append(value[name_field])
        return tuple(names)


class GitHubProjectAdapter:
    """Obtém itens do Project configurado sem alterar o GitHub."""

    def __init__(
        self,
        config: GitHubConfig | OrchestratorConfig,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config.github if isinstance(config, OrchestratorConfig) else config
        self.runner = (
            runner
            if runner is not None
            else CommandRunner(timeout=GITHUB_PROJECT_TIMEOUT_SECONDS)
        )

    def list_items(self) -> tuple[ProjectItem, ...]:
        """Carrega os itens do Project por meio do GitHub CLI autenticado."""
        arguments = [
            "gh", "project", "item-list", str(self.config.project_number), "--owner",
            self.config.owner, "--format", "json",
        ]
        result = self.runner.run(arguments)
        if result.error:
            raise GitHubProjectError(
                f"Não foi possível executar o GitHub CLI para ler o Project: {result.error}"
            )
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = "GitHub CLI retornou código {} ao ler o Project {}".format(
                result.returncode, self.config.project_number
            )
            if detail:
                message = f"{message}: {detail}"
            raise GitHubProjectError(message)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubProjectError("GitHub CLI retornou JSON inválido para o Project") from error

        return self._parse_items(payload)

    @classmethod
    def _parse_items(cls, payload: Any) -> tuple[ProjectItem, ...]:
        if not isinstance(payload, dict):
            raise GitHubProjectError("Resposta do Project inválida: objeto JSON esperado")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise GitHubProjectError("Resposta do Project inválida: campo 'items' deve ser uma lista")
        return tuple(cls._parse_item(raw_item) for raw_item in raw_items)

    @classmethod
    def _parse_item(cls, payload: Any) -> ProjectItem:
        if not isinstance(payload, dict):
            raise GitHubProjectError("Resposta do Project inválida: item deve ser um objeto")
        item_id = cls._required_string(payload, "id")
        content = payload.get("content")
        if not isinstance(content, dict):
            raise GitHubProjectError(
                "Resposta do Project inválida: campo 'content' deve ser um objeto"
            )
        content_type = cls._required_string(content, "type")
        optional_fields = {
            field: cls._optional_string(payload, field)
            for field in ("status", "priority", "size", "risk", "agent")
        }
        if content_type != "Issue":
            return ProjectItem(
                id=item_id,
                content_type=content_type,
                issue_number=None,
                title=cls._optional_string(content, "title"),
                url=cls._optional_string(content, "url"),
                repository=cls._optional_string(content, "repository"),
                **optional_fields,
            )
        return ProjectItem(
            id=item_id,
            content_type=content_type,
            issue_number=cls._required_int(content, "number"),
            title=cls._required_string(content, "title"),
            url=cls._required_string(content, "url"),
            repository=cls._required_string(content, "repository"),
            **optional_fields,
        )

    @staticmethod
    def _required_int(payload: dict[str, Any], field: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise GitHubProjectError(
                f"Resposta do Project inválida: campo '{field}' deve ser inteiro"
            )
        return value

    @staticmethod
    def _required_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str):
            raise GitHubProjectError(
                f"Resposta do Project inválida: campo '{field}' deve ser texto"
            )
        return value

    @staticmethod
    def _optional_string(payload: dict[str, Any], field: str) -> str | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise GitHubProjectError(
                f"Resposta do Project inválida: campo '{field}' deve ser texto ou nulo"
            )
        return value
