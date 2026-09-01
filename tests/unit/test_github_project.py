"""Testes unitários da leitura read-only de GitHub Projects."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest

from ai_dev_orchestrator.adapters.github import (
    GITHUB_PROJECT_ITEM_LIMIT,
    GitHubProjectAdapter,
    GitHubProjectError,
)
from ai_dev_orchestrator.config import GitHubConfig
from ai_dev_orchestrator.domain.project import is_eligible_for_execution
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


@dataclass
class FakeRunner:
    result: CommandResult
    arguments: list[str] = field(default_factory=list)

    def run(self, arguments: list[str]) -> CommandResult:
        self.arguments = arguments
        return self.result


def github_config(ready_status: str = "Ready") -> GitHubConfig:
    return GitHubConfig(
        owner="acme", repository="orchestrator", project_number=6, ready_status=ready_status
    )


def project_payload(item: object | None = None) -> str:
    if item is None:
        item = {
            "id": "PVTI_1",
            "content": {
                "type": "Issue", "number": 9, "title": "Ler Project",
                "url": "https://github.com/acme/orchestrator/issues/9",
                "repository": "acme/orchestrator",
            },
            "status": "Ready", "priority": "High", "size": "M",
            "risk": "Low", "agent": "Codex",
        }
    return json.dumps({"items": [item]})


def read_item(payload: str | None = None):
    return GitHubProjectAdapter(
        github_config(), FakeRunner(CommandResult(0, payload or project_payload()))
    ).list_items()[0]


def test_reads_and_maps_project_item() -> None:
    runner = FakeRunner(CommandResult(0, project_payload()))
    item = GitHubProjectAdapter(github_config(), runner).list_items()[0]

    assert item.id == "PVTI_1"
    assert (item.issue_number, item.title, item.url, item.repository) == (
        9, "Ler Project", "https://github.com/acme/orchestrator/issues/9", "acme/orchestrator"
    )
    assert (item.status, item.priority, item.size, item.risk, item.agent) == (
        "Ready", "High", "M", "Low", "Codex"
    )
    assert runner.arguments == [
        "gh", "project", "item-list", "6", "--owner", "acme", "--limit",
        str(GITHUB_PROJECT_ITEM_LIMIT), "--format", "json"
    ]
    assert isinstance(runner.arguments, list)


def test_uses_remote_timeout_only_for_project_default_runner() -> None:
    adapter = GitHubProjectAdapter(github_config())

    assert isinstance(adapter.runner, CommandRunner)
    assert adapter.runner.timeout == 20
    assert CommandRunner().timeout == 5


def test_normalizes_missing_optional_project_fields_to_none() -> None:
    payload = json.loads(project_payload())
    for optional_field in ("status", "priority", "size", "risk", "agent"):
        payload["items"][0].pop(optional_field)

    item = read_item(json.dumps(payload))

    assert (item.status, item.priority, item.size, item.risk, item.agent) == (None,) * 5


def test_non_issue_is_explicitly_represented_and_not_eligible() -> None:
    payload = json.loads(project_payload())
    payload["items"][0]["content"] = {"type": "PullRequest", "title": "PR"}
    item = read_item(json.dumps(payload))

    assert not item.is_issue
    assert item.issue_number is None
    assert not is_eligible_for_execution(item, "acme/orchestrator", "Ready")


def test_item_from_another_repository_is_not_eligible() -> None:
    payload = json.loads(project_payload())
    payload["items"][0]["content"]["repository"] = "other/repository"

    assert not is_eligible_for_execution(read_item(json.dumps(payload)), "acme/orchestrator", "Ready")


def test_ready_item_is_eligible_using_configured_status() -> None:
    item = read_item()

    assert is_eligible_for_execution(item, "acme/orchestrator", github_config().ready_status)
    assert not is_eligible_for_execution(item, "acme/orchestrator", "Pronto")


def test_status_comparison_is_exact() -> None:
    payload = json.loads(project_payload())
    payload["items"][0]["status"] = "ready"

    assert not is_eligible_for_execution(read_item(json.dumps(payload)), "acme/orchestrator", "Ready")


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(None, error="Executável não encontrado: gh"), "GitHub CLI.*não encontrado"),
        (CommandResult(1, stderr="Project not found"), "código 1.*Project not found"),
    ],
)
def test_reports_github_cli_failures(result: CommandResult, message: str) -> None:
    with pytest.raises(GitHubProjectError, match=message):
        GitHubProjectAdapter(github_config(), FakeRunner(result)).list_items()


def test_reports_invalid_json_with_original_cause() -> None:
    with pytest.raises(GitHubProjectError, match="JSON inválido") as error:
        GitHubProjectAdapter(github_config(), FakeRunner(CommandResult(0, "invalid"))).list_items()

    assert error.value.__cause__ is not None


def test_requests_more_than_the_gh_default_project_item_limit() -> None:
    runner = FakeRunner(CommandResult(0, project_payload()))

    GitHubProjectAdapter(github_config(), runner).list_items()

    assert int(runner.arguments[runner.arguments.index("--limit") + 1]) > 30


def test_reports_possible_project_truncation_at_explicit_limit() -> None:
    item = json.loads(project_payload())["items"][0]
    payload = json.dumps({"items": [item] * GITHUB_PROJECT_ITEM_LIMIT})

    with pytest.raises(GitHubProjectError, match="pode estar truncada"):
        GitHubProjectAdapter(github_config(), FakeRunner(CommandResult(0, payload))).list_items()


@pytest.mark.parametrize(
    "payload",
    [
        "[]", "{}", '{"items": {}}', '{"items": [null]}',
        '{"items": [{"id": "item", "content": null}]}',
        '{"items": [{"id": "item", "content": {"type": "Issue"}}]}',
    ],
)
def test_rejects_incomplete_or_unexpected_project_payload(payload: str) -> None:
    with pytest.raises(GitHubProjectError, match="Resposta do Project inválida"):
        GitHubProjectAdapter(github_config(), FakeRunner(CommandResult(0, payload))).list_items()
