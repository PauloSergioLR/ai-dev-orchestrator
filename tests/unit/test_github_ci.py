"""Testes da consulta estruturada de CI por GitHub CLI."""

from dataclasses import dataclass, field

import pytest

from ai_dev_orchestrator.adapters.github import GitHubCiAdapter, GitHubCiError
from ai_dev_orchestrator.config import GitHubConfig
from ai_dev_orchestrator.infrastructure.process import CommandResult


@dataclass
class Runner:
    results: list[CommandResult]
    calls: list[list[str]] = field(default_factory=list)

    def run(self, arguments: list[str]) -> CommandResult:
        self.calls.append(arguments)
        return self.results.pop(0)


def test_reads_head_and_rollup_from_configured_pr_and_repository() -> None:
    runner = Runner([CommandResult(0, '{"headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","statusCheckRollup":[{"name":"test","status":"COMPLETED","conclusion":"SUCCESS","detailsUrl":"https://ci"}]}')])
    adapter = GitHubCiAdapter(GitHubConfig(owner="acme", repository="repo", project_number=1, ready_status="Ready"), runner)
    snapshot = adapter.get_ci_snapshot(42)
    assert snapshot.head_sha == "a" * 40
    assert snapshot.checks[0].name == "test"
    assert runner.calls == [["gh", "pr", "view", "42", "--repo", "acme/repo", "--json", "headRefOid,statusCheckRollup"]]


@pytest.mark.parametrize(
    ("state", "status", "conclusion"),
    [
        ("PENDING", "PENDING", None),
        ("SUCCESS", "COMPLETED", "SUCCESS"),
        ("FAILURE", "COMPLETED", "FAILURE"),
        ("ERROR", "COMPLETED", "ERROR"),
        ("UNEXPECTED", "COMPLETED", "UNKNOWN"),
    ],
)
def test_normalizes_status_context(state: str, status: str, conclusion: str | None) -> None:
    runner = Runner([CommandResult(0, '{"headRefOid":"' + "a" * 40 + '","statusCheckRollup":[{"context":"legacy","state":"' + state + '","targetUrl":"https://legacy"}]}')])
    adapter = GitHubCiAdapter(GitHubConfig(owner="acme", repository="repo", project_number=1, ready_status="Ready"), runner)
    snapshot = adapter.get_ci_snapshot(42)
    assert snapshot.checks[0].name == "legacy"
    assert (snapshot.checks[0].status, snapshot.checks[0].conclusion, snapshot.checks[0].details_url) == (status, conclusion, "https://legacy")


def test_accepts_optional_status_context_with_required_check_run() -> None:
    payload = '{"headRefOid":"' + "a" * 40 + '","statusCheckRollup":[{"context":"optional","state":"PENDING","targetUrl":"https://legacy"},{"name":"test","status":"COMPLETED","conclusion":"SUCCESS","detailsUrl":"https://ci"}]}'
    runner = Runner([CommandResult(0, payload)])
    adapter = GitHubCiAdapter(GitHubConfig(owner="acme", repository="repo", project_number=1, ready_status="Ready"), runner)
    snapshot = adapter.get_ci_snapshot(42)
    assert [(check.name, check.status) for check in snapshot.checks] == [("optional", "PENDING"), ("test", "COMPLETED")]


@pytest.mark.parametrize("payload", ['{"headRefOid":"","statusCheckRollup":[]}', '{"headRefOid":"not-a-sha","statusCheckRollup":[]}'])
def test_rejects_missing_or_invalid_head(payload: str) -> None:
    runner = Runner([CommandResult(0, payload)])
    adapter = GitHubCiAdapter(GitHubConfig(owner="acme", repository="repo", project_number=1, ready_status="Ready"), runner)
    with pytest.raises(GitHubCiError, match="headRefOid"):
        adapter.get_ci_snapshot(42)
