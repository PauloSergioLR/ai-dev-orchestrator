"""Adapters para issues e GitHub Projects via GitHub CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
import re
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.config import GitHubConfig, OrchestratorConfig
from ai_dev_orchestrator.domain.ci import PullRequestCiSnapshot, StatusCheck
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import (
    ProjectItem,
    ProjectMetadata,
    ProjectStatusField,
    ProjectStatusOption,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.services.validation import GateResult


GITHUB_ISSUE_TIMEOUT_SECONDS = 20
GITHUB_PROJECT_TIMEOUT_SECONDS = 20
GITHUB_PROJECT_ITEM_LIMIT = 1000
GITHUB_PULL_REQUEST_TIMEOUT_SECONDS = 30
GITHUB_CI_TIMEOUT_SECONDS = 30


class GitHubIssueError(Exception):
    """Indica que uma issue não pôde ser carregada do GitHub."""


class GitHubProjectError(Exception):
    """Indica que os itens de um GitHub Project não puderam ser carregados."""


class GitHubProjectStatusError(Exception):
    """Indica que o Status de um item do GitHub Project não pôde ser atualizado."""


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


class GitHubPullRequestError(Exception):
    """Indica que um Pull Request não pôde ser criado."""


class GitHubCiError(Exception):
    """Indica que a CI de um Pull Request não pôde ser consultada com segurança."""


@dataclass(frozen=True)
class PullRequest:
    """Dados mínimos do Pull Request publicado."""

    number: int
    url: str
    title: str
    base: str
    head: str


def build_pull_request_body(issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> str:
    """Monta o texto determinístico do Pull Request sem usar saída do Codex."""
    gates_text = "\n".join(f"- {gate.name}: {'aprovado' if gate.succeeded else 'reprovado'}" for gate in gates)
    return (
        "Pull Request criado automaticamente pelo AI Dev Orchestrator.\n\n"
        f"Issue: #{issue.number}\n"
        f"Branch: `{branch}`\n"
        "Implementação produzida pelo Codex.\n\n"
        "Gates locais executados:\n"
        f"{gates_text}\n\n"
        f"Closes #{issue.number}"
    )


class GitHubPullRequestAdapter:
    """Cria Pull Requests por GitHub CLI com argumentos seguros."""

    def __init__(self, config: GitHubConfig | OrchestratorConfig, runner: ProcessRunner | None = None) -> None:
        self.config = config.github if isinstance(config, OrchestratorConfig) else config
        self.runner = runner or CommandRunner(timeout=GITHUB_PULL_REQUEST_TIMEOUT_SECONDS)

    def create(self, issue: Issue, branch: str, gates: tuple[GateResult, ...]) -> PullRequest:
        arguments = [
            "gh", "pr", "create", "--repo", self.config.repository_full_name,
            "--base", self.config.pull_request_base, "--head", branch,
            "--title", issue.title, "--body", build_pull_request_body(issue, branch, gates),
        ]
        result = self.runner.run(arguments)
        if result.error:
            raise GitHubPullRequestError(f"Não foi possível executar o GitHub CLI ao criar o Pull Request: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"GitHub CLI retornou código {result.returncode} ao criar o Pull Request"
            raise GitHubPullRequestError(f"{message}: {detail}" if detail else message)
        url, number = self._parse_created_url(result.stdout)
        view = self.runner.run(["gh", "pr", "view", url, "--repo", self.config.repository_full_name,
                                "--json", "title,baseRefName,headRefName"])
        if view.error or not view.succeeded:
            detail = view.error or view.stderr.strip() or view.stdout.strip()
            raise GitHubPullRequestError(
                f"Pull Request #{number} já criado em {url}, mas não foi possível consultar seus dados: {detail}"
            )
        try:
            payload = json.loads(view.stdout)
            return PullRequest(number, url, self._required_string(payload, "title"),
                               self._required_string(payload, "baseRefName"), self._required_string(payload, "headRefName"))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GitHubPullRequestError(
                f"Pull Request #{number} já criado em {url}, mas o GitHub CLI retornou JSON inválido"
            ) from error

    def _parse_created_url(self, stdout: str) -> tuple[str, int]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise GitHubPullRequestError("GitHub CLI não retornou uma única URL válida para o Pull Request")
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(self.config.owner)}/{re.escape(self.config.repository)}/pull/([1-9][0-9]*)",
            lines[0],
        )
        if match is None:
            raise GitHubPullRequestError("GitHub CLI não retornou uma URL válida para o Pull Request")
        return lines[0], int(match.group(1))

    def get_review_data(self, pull_request_number: int) -> dict[str, Any]:
        """Lê os dados de um único PR pelo CLI estruturado e seu patch real."""
        view = self.runner.run([
            "gh", "pr", "view", str(pull_request_number), "--repo", self.config.repository_full_name,
            "--json", "number,url,state,baseRefName,headRefName,headRefOid,changedFiles,files",
        ])
        if view.error or not view.succeeded:
            detail = view.error or view.stderr.strip() or view.stdout.strip()
            raise GitHubPullRequestError(f"Não foi possível ler Pull Request #{pull_request_number}: {detail}")
        try:
            payload = json.loads(view.stdout)
        except json.JSONDecodeError as error:
            raise GitHubPullRequestError("GitHub CLI retornou JSON inválido para o Pull Request") from error
        if not isinstance(payload, dict) or payload.get("number") != pull_request_number:
            raise GitHubPullRequestError("Resposta do Pull Request não corresponde ao número solicitado")
        if not isinstance(payload.get("changedFiles"), int) or isinstance(payload["changedFiles"], bool) or payload["changedFiles"] < 0:
            raise GitHubPullRequestError("Resposta do Pull Request inválida: campo 'changedFiles'")
        if not isinstance(payload.get("files"), list):
            raise GitHubPullRequestError("Resposta do Pull Request inválida: campo 'files'")
        if len(payload["files"]) != payload["changedFiles"]:
            raise GitHubPullRequestError("Lista de arquivos do Pull Request está truncada ou incompleta")
        commits_result = self.runner.run([
            "gh", "api", "--paginate", "--slurp", f"repos/{self.config.repository_full_name}/pulls/{pull_request_number}/commits",
        ])
        if commits_result.error or not commits_result.succeeded:
            detail = commits_result.error or commits_result.stderr.strip() or commits_result.stdout.strip()
            raise GitHubPullRequestError(f"Não foi possível ler todos os commits do Pull Request #{pull_request_number}: {detail}")
        try:
            raw_pages = json.loads(commits_result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubPullRequestError("GitHub CLI retornou JSON inválido para commits paginados") from error
        if not isinstance(raw_pages, list) or not all(isinstance(page, list) for page in raw_pages):
            raise GitHubPullRequestError("Resposta paginada de commits inválida")
        payload["commits"] = [commit for page in raw_pages for commit in page]
        if not payload["commits"]:
            raise GitHubPullRequestError("Resposta paginada de commits está vazia")
        for field in ("commits", "files"):
            if not isinstance(payload.get(field), list):
                raise GitHubPullRequestError(f"Resposta do Pull Request inválida: campo '{field}'")
        try:
            payload["commits"] = [self._required_sha(x) for x in payload["commits"]]
            payload["files"] = [self._required_string(x, "path") for x in payload["files"]]
        except (TypeError, ValueError) as error:
            raise GitHubPullRequestError(
                "Resposta REST paginada contém commits ou arquivos inválidos"
            ) from error
        diff = self.runner.run(["gh", "pr", "diff", str(pull_request_number), "--repo", self.config.repository_full_name])
        if diff.error or not diff.succeeded:
            detail = diff.error or diff.stderr.strip() or diff.stdout.strip()
            raise GitHubPullRequestError(f"Não foi possível ler diff do Pull Request #{pull_request_number}: {detail}")
        if not diff.stdout:
            raise GitHubPullRequestError("Diff do Pull Request está vazio ou foi truncado externamente")
        observed_headers = [line for line in diff.stdout.splitlines() if line.startswith("diff --git a/")]
        if len(observed_headers) != payload["changedFiles"]:
            raise GitHubPullRequestError(
                "Diff do Pull Request não contém a quantidade completa de arquivos; revisão recusada"
            )
        payload["diff"] = diff.stdout
        return payload

    @staticmethod
    def _required_string(payload: Any, field: str) -> str:
        value = payload[field]
        if not isinstance(value, str):
            raise ValueError(field)
        return value

    @staticmethod
    def _required_sha(payload: Any) -> str:
        """Valida o contrato REST de ``GET /pulls/{number}/commits``."""
        if not isinstance(payload, dict):
            raise ValueError("commit")
        sha = payload.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
            raise ValueError("sha")
        return sha


class GitHubCiAdapter:
    """Lê HEAD e checks por ``gh``; a decisão de aprovação fica no serviço."""

    def __init__(
        self, config: GitHubConfig | OrchestratorConfig, runner: ProcessRunner | None = None
    ) -> None:
        self.config = config.github if isinstance(config, OrchestratorConfig) else config
        self.runner = runner or CommandRunner(timeout=GITHUB_CI_TIMEOUT_SECONDS)

    def get_ci_snapshot(self, pull_request_number: int) -> PullRequestCiSnapshot:
        arguments = [
            "gh", "pr", "view", str(pull_request_number), "--repo",
            self.config.repository_full_name, "--json", "headRefOid,statusCheckRollup",
        ]
        result = self.runner.run(arguments)
        if result.error:
            raise GitHubCiError(f"Não foi possível executar o GitHub CLI ao consultar a CI: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"GitHub CLI retornou código {result.returncode} ao consultar a CI do Pull Request #{pull_request_number}"
            raise GitHubCiError(f"{message}: {detail}" if detail else message)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubCiError("GitHub CLI retornou JSON inválido para a CI do Pull Request") from error
        return self._parse_snapshot(payload)

    @classmethod
    def _parse_snapshot(cls, payload: Any) -> PullRequestCiSnapshot:
        if not isinstance(payload, dict):
            raise GitHubCiError("Resposta da CI inválida: objeto JSON esperado")
        head_sha = payload.get("headRefOid")
        if not isinstance(head_sha, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head_sha
        ):
            raise GitHubCiError("Resposta da CI inválida: campo 'headRefOid' deve ser um SHA válido")
        raw_checks = payload.get("statusCheckRollup")
        if raw_checks is None:
            raw_checks = []
        if not isinstance(raw_checks, list):
            raise GitHubCiError("Resposta da CI inválida: campo 'statusCheckRollup' deve ser uma lista")
        return PullRequestCiSnapshot(head_sha, tuple(cls._parse_check(check) for check in raw_checks))

    @classmethod
    def _parse_check(cls, payload: Any) -> StatusCheck:
        if not isinstance(payload, dict):
            raise GitHubCiError("Resposta da CI inválida: cada check deve ser um objeto")
        if "context" in payload:
            return cls._parse_status_context(payload)
        name = payload.get("name")
        status = payload.get("status")
        conclusion = payload.get("conclusion")
        details_url = payload.get("detailsUrl")
        if not isinstance(name, str) or not name:
            raise GitHubCiError("Resposta da CI inválida: check sem nome")
        if not isinstance(status, str) or not status:
            raise GitHubCiError("Resposta da CI inválida: check sem status")
        if conclusion is not None and not isinstance(conclusion, str):
            raise GitHubCiError("Resposta da CI inválida: conclusão do check deve ser texto ou nula")
        if details_url is not None and not isinstance(details_url, str):
            raise GitHubCiError("Resposta da CI inválida: URL de detalhes do check deve ser texto ou nula")
        return StatusCheck(name, status, conclusion, details_url)

    @staticmethod
    def _parse_status_context(payload: dict[str, Any]) -> StatusCheck:
        """Normaliza o membro ``StatusContext`` da union statusCheckRollup."""
        name = payload.get("context")
        state = payload.get("state")
        details_url = payload.get("targetUrl")
        if not isinstance(name, str) or not name:
            raise GitHubCiError("Resposta da CI inválida: StatusContext sem context")
        if not isinstance(state, str) or not state:
            raise GitHubCiError("Resposta da CI inválida: StatusContext sem state")
        if details_url is not None and not isinstance(details_url, str):
            raise GitHubCiError("Resposta da CI inválida: targetUrl do StatusContext deve ser texto ou nula")
        normalized_state = state.upper()
        if normalized_state in {"EXPECTED", "PENDING"}:
            return StatusCheck(name, "PENDING", None, details_url)
        if normalized_state == "SUCCESS":
            return StatusCheck(name, "COMPLETED", "SUCCESS", details_url)
        if normalized_state in {"FAILURE", "ERROR"}:
            return StatusCheck(name, "COMPLETED", normalized_state, details_url)
        return StatusCheck(name, "COMPLETED", "UNKNOWN", details_url)


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
            self.config.owner, "--limit", str(GITHUB_PROJECT_ITEM_LIMIT), "--format", "json",
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

        items = self._parse_items(payload)
        if len(items) >= GITHUB_PROJECT_ITEM_LIMIT:
            raise GitHubProjectError(
                "A leitura do Project atingiu o limite de "
                f"{GITHUB_PROJECT_ITEM_LIMIT} itens e pode estar truncada"
            )
        return items

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


class GitHubProjectStatusAdapter:
    """Atualiza exclusivamente o campo Status de um item explicitamente informado."""

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

    def set_status(self, project_item_id: str, status_name: str) -> None:
        """Resolve IDs atuais e grava apenas o Status do item informado."""
        if not isinstance(project_item_id, str) or not project_item_id:
            raise GitHubProjectStatusError("ID do item do Project deve ser texto não vazio")
        if not isinstance(status_name, str) or not status_name:
            raise GitHubProjectStatusError("Status solicitado deve ser texto não vazio")

        project = self.discover_project_metadata()
        status_field = self.resolve_status_field()
        status_option = self.resolve_status_option(status_field, status_name)
        self.execute_status_mutation(project_item_id, project, status_field, status_option)

    def discover_project_metadata(self) -> ProjectMetadata:
        """Descobre o ID global do Project configurado sem fazer mutações."""
        payload = self._run_json(
            [
                "gh", "project", "view", str(self.config.project_number), "--owner",
                self.config.owner, "--format", "json",
            ],
            "descobrir os metadados do Project",
        )
        return self._parse_project_metadata(payload)

    def resolve_status_field(self) -> ProjectStatusField:
        """Localiza o campo Status configurado entre os campos reais do Project."""
        payload = self._run_json(
            [
                "gh", "project", "field-list", str(self.config.project_number), "--owner",
                self.config.owner, "--format", "json",
            ],
            "descobrir os campos do Project",
        )
        field = self._parse_status_field(payload, self.config.status_field_name)
        if field is not None:
            return field
        raise GitHubProjectStatusError(
            f"Campo Status '{self.config.status_field_name}' não encontrado no Project"
        )

    @staticmethod
    def resolve_status_option(
        status_field: ProjectStatusField, status_name: str
    ) -> ProjectStatusOption:
        """Resolve por igualdade exata a opção que será escrita."""
        for option in status_field.options:
            if option.name == status_name:
                return option
        raise GitHubProjectStatusError(
            f"Status '{status_name}' não encontrado no campo '{status_field.name}'"
        )

    def execute_status_mutation(
        self,
        project_item_id: str,
        project: ProjectMetadata,
        status_field: ProjectStatusField,
        status_option: ProjectStatusOption,
    ) -> None:
        """Executa a única mutação permitida: uma opção do campo Status."""
        arguments = [
            "gh", "project", "item-edit", "--id", project_item_id,
            "--project-id", project.id, "--field-id", status_field.id,
            "--single-select-option-id", status_option.id,
        ]
        self._run(arguments, "atualizar o Status do item do Project")

    def _run_json(self, arguments: list[str], operation: str) -> Any:
        result = self._run(arguments, operation)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubProjectStatusError(
                f"GitHub CLI retornou JSON inválido ao {operation}"
            ) from error

    def _run(self, arguments: list[str], operation: str) -> CommandResult:
        result = self.runner.run(arguments)
        if result.error:
            raise GitHubProjectStatusError(
                f"Não foi possível executar o GitHub CLI ao {operation}: {result.error}"
            )
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"GitHub CLI retornou código {result.returncode} ao {operation}"
            if detail:
                message = f"{message}: {detail}"
            raise GitHubProjectStatusError(message)
        return result

    @staticmethod
    def _parse_project_metadata(payload: Any) -> ProjectMetadata:
        if not isinstance(payload, dict):
            raise GitHubProjectStatusError(
                "Metadados do Project inválidos: objeto JSON esperado"
            )
        return ProjectMetadata(
            id=GitHubProjectStatusAdapter._required_string(payload, "id", "metadados do Project")
        )

    @classmethod
    def _parse_status_field(
        cls, payload: Any, status_field_name: str
    ) -> ProjectStatusField | None:
        if not isinstance(payload, dict):
            raise GitHubProjectStatusError(
                "Campos do Project inválidos: objeto JSON esperado"
            )
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, list):
            raise GitHubProjectStatusError(
                "Campos do Project inválidos: campo 'fields' deve ser uma lista"
            )
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                raise GitHubProjectStatusError(
                    "Campos do Project inválidos: cada campo deve ser um objeto"
                )
            name = cls._required_string(raw_field, "name", "campos do Project")
            if name == status_field_name:
                return cls._parse_project_field(raw_field)
        return None

    @classmethod
    def _parse_project_field(cls, payload: Any) -> ProjectStatusField:
        if not isinstance(payload, dict):
            raise GitHubProjectStatusError(
                "Campos do Project inválidos: cada campo deve ser um objeto"
            )
        raw_options = payload.get("options")
        if not isinstance(raw_options, list):
            raise GitHubProjectStatusError(
                "Campos do Project inválidos: campo 'options' deve ser uma lista"
            )
        options = tuple(cls._parse_status_option(option) for option in raw_options)
        return ProjectStatusField(
            id=cls._required_string(payload, "id", "campos do Project"),
            name=cls._required_string(payload, "name", "campos do Project"),
            options=options,
        )

    @classmethod
    def _parse_status_option(cls, payload: Any) -> ProjectStatusOption:
        if not isinstance(payload, dict):
            raise GitHubProjectStatusError(
                "Opções de Status inválidas: cada opção deve ser um objeto"
            )
        return ProjectStatusOption(
            id=cls._required_string(payload, "id", "opções de Status"),
            name=cls._required_string(payload, "name", "opções de Status"),
        )

    @staticmethod
    def _required_string(payload: dict[str, Any], field: str, context: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise GitHubProjectStatusError(
                f"Resposta de {context} inválida: campo '{field}' deve ser texto não vazio"
            )
        return value
