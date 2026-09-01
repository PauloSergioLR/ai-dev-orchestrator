"""Testes unitários do adapter de leitura de issues do GitHub."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ai_dev_orchestrator.adapters.github import GitHubIssueAdapter, GitHubIssueError
from ai_dev_orchestrator.config import GitHubConfig
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


@dataclass
class FakeRunner:
    result: CommandResult
    arguments: list[str] = field(default_factory=list)

    def run(self, arguments: list[str]) -> CommandResult:
        self.arguments = arguments
        return self.result


def github_config() -> GitHubConfig:
    return GitHubConfig(
        owner="acme", repository="orchestrator", project_number=1, ready_status="Ready"
    )


def valid_payload(body: object = "Descrição") -> str:
    serialized_body = "null" if body is None else repr(body).replace("'", '"')
    return (
        '{"number":7,"title":"Adapter GitHub","body":'
        + serialized_body
        + ',"state":"OPEN","url":"https://github.com/acme/orchestrator/issues/7",'
        '"labels":[{"name":"backend"},{"name":"github"}],'
        '"assignees":[{"login":"ana"},{"login":"bia"}]}'
    )


def test_reads_and_converts_issue_from_configured_repository() -> None:
    runner = FakeRunner(CommandResult(0, valid_payload()))

    issue = GitHubIssueAdapter(github_config(), runner).get_issue(7)

    assert issue.number == 7
    assert issue.title == "Adapter GitHub"
    assert issue.body == "Descrição"
    assert issue.state == "OPEN"
    assert issue.url == "https://github.com/acme/orchestrator/issues/7"
    assert issue.labels == ("backend", "github")
    assert issue.assignees == ("ana", "bia")
    assert runner.arguments == [
        "gh", "issue", "view", "7", "--repo", "acme/orchestrator", "--json",
        "number,title,body,state,url,labels,assignees",
    ]
    assert isinstance(runner.arguments, list)


def test_uses_twenty_second_timeout_only_for_default_runner() -> None:
    adapter = GitHubIssueAdapter(github_config())
    custom_runner = FakeRunner(CommandResult(0, valid_payload()))

    assert isinstance(adapter.runner, CommandRunner)
    assert adapter.runner.timeout == 20
    assert GitHubIssueAdapter(github_config(), custom_runner).runner is custom_runner


def test_normalizes_missing_body_to_empty_string() -> None:
    runner = FakeRunner(CommandResult(0, valid_payload(None)))

    assert GitHubIssueAdapter(github_config(), runner).get_issue(7).body == ""


def test_reports_missing_github_cli() -> None:
    runner = FakeRunner(CommandResult(None, error="Executável não encontrado: gh"))

    with pytest.raises(GitHubIssueError, match="GitHub CLI.*não encontrado"):
        GitHubIssueAdapter(github_config(), runner).get_issue(7)


def test_reports_github_failure_including_issue_inaccessible() -> None:
    runner = FakeRunner(CommandResult(1, stderr="could not resolve issue"))

    with pytest.raises(GitHubIssueError, match="código 1.*could not resolve issue"):
        GitHubIssueAdapter(github_config(), runner).get_issue(7)


def test_reports_invalid_json_with_original_cause() -> None:
    runner = FakeRunner(CommandResult(0, "not-json"))

    with pytest.raises(GitHubIssueError, match="JSON inválido") as error:
        GitHubIssueAdapter(github_config(), runner).get_issue(7)

    assert error.value.__cause__ is not None


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"number":"7","title":"x","body":"","state":"OPEN","url":"x","labels":[],"assignees":[]}',
        '{"number":7,"title":"x","body":"","state":"OPEN","url":"x","labels":{},"assignees":[]}',
        '{"number":7,"title":"x","body":"","state":"OPEN","url":"x","labels":[],"assignees":[{}]}',
    ],
)
def test_rejects_incomplete_or_invalid_payload(payload: str) -> None:
    runner = FakeRunner(CommandResult(0, payload))

    with pytest.raises(GitHubIssueError, match="Resposta da issue inválida"):
        GitHubIssueAdapter(github_config(), runner).get_issue(7)
