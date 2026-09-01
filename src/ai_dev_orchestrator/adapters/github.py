"""Adapter somente de leitura para issues do GitHub via GitHub CLI."""

from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.config import GitHubConfig, OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


GITHUB_ISSUE_TIMEOUT_SECONDS = 20


class GitHubIssueError(Exception):
    """Indica que uma issue não pôde ser carregada do GitHub."""


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
