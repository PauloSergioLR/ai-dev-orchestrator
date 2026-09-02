"""Testes do contrato suportado de criação e leitura de Pull Request."""

from dataclasses import dataclass, field
import json

import pytest

from ai_dev_orchestrator.adapters.github import GitHubPullRequestAdapter, GitHubPullRequestError
from ai_dev_orchestrator.config import GitHubConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.validation import GateResult


@dataclass
class Runner:
    results: list[CommandResult]
    calls: list[list[str]] = field(default_factory=list)
    def run(self, arguments: list[str]) -> CommandResult:
        self.calls.append(arguments)
        return self.results.pop(0)


def adapter(results: list[CommandResult]) -> tuple[GitHubPullRequestAdapter, Runner]:
    runner = Runner(results)
    config = GitHubConfig(owner="acme", repository="repo", project_number=1, ready_status="Ready", pull_request_base="release")
    return GitHubPullRequestAdapter(config, runner), runner


def create(adapter_value: GitHubPullRequestAdapter):
    return adapter_value.create(Issue(19, "Título original", "", "OPEN", "url", (), ()), "feat/test", (GateResult("ruff", ("uv",), True, 0, ""),))


def test_create_uses_supported_commands_and_returns_metadata() -> None:
    value, runner = adapter([CommandResult(0, "https://github.com/acme/repo/pull/42\n"), CommandResult(0, '{"title":"Título original","baseRefName":"release","headRefName":"feat/test"}')])
    pr = create(value)
    assert (pr.number, pr.url, pr.title, pr.base, pr.head) == (42, "https://github.com/acme/repo/pull/42", "Título original", "release", "feat/test")
    assert "--json" not in runner.calls[0]
    assert runner.calls[1] == ["gh", "pr", "view", pr.url, "--repo", "acme/repo", "--json", "title,baseRefName,headRefName"]
    body = runner.calls[0][runner.calls[0].index("--body") + 1]
    assert "feat/test" in body and "Codex" in body and "ruff" in body and "Closes #19" in body
    assert runner.calls[0][runner.calls[0].index("--base") + 1] == "release"


@pytest.mark.parametrize("stdout", ["texto qualquer\n", "https://github.com/other/repo/pull/2\n", "https://github.com/acme/repo/pull/0\n"])
def test_rejects_invalid_or_other_repository_url(stdout: str) -> None:
    value, runner = adapter([CommandResult(0, stdout)])
    with pytest.raises(GitHubPullRequestError, match="URL válida"):
        create(value)
    assert len(runner.calls) == 1


def test_view_failure_reports_created_pr_url_and_number() -> None:
    value, _ = adapter([CommandResult(0, "https://github.com/acme/repo/pull/42\n"), CommandResult(1, stderr="sem permissão")])
    with pytest.raises(GitHubPullRequestError, match=r"#42.*pull/42"):
        create(value)


def test_review_data_uses_pagination_and_accepts_renamed_diff() -> None:
    metadata = '{"number":42,"url":"u","baseRefName":"main","headRefName":"f","headRefOid":"' + "a" * 40 + '","changedFiles":1,"files":[{"path":"new.py"}]}'
    commits = '[[{"sha":"' + "a" * 40 + '"}],[{"sha":"' + "b" * 64 + '"}]]'
    value, runner = adapter([CommandResult(0, metadata), CommandResult(0, commits), CommandResult(0, "diff --git a/old.py b/new.py\nsimilarity index 90%")])
    result = value.get_review_data(42)
    assert result["commits"] == ["a" * 40, "b" * 64]
    assert "--paginate" in runner.calls[1] and "--slurp" in runner.calls[1]


def test_review_data_rejects_truncated_file_list() -> None:
    metadata = '{"number":42,"url":"u","baseRefName":"main","headRefName":"f","headRefOid":"' + "a" * 40 + '","changedFiles":101,"files":[]}'
    value, _ = adapter([CommandResult(0, metadata)])
    with pytest.raises(GitHubPullRequestError, match="truncada"):
        value.get_review_data(42)


@pytest.mark.parametrize("commit", [{}, {"sha": ""}, {"sha": 7}, {"sha": "not-a-sha"}])
def test_review_data_rejects_missing_or_invalid_rest_commit_sha(commit: dict) -> None:
    metadata = '{"number":42,"url":"u","baseRefName":"main","headRefName":"f","headRefOid":"' + "a" * 40 + '","changedFiles":1,"files":[{"path":"new.py"}]}'
    value, runner = adapter([CommandResult(0, metadata), CommandResult(0, json.dumps([[commit]]))])
    with pytest.raises(GitHubPullRequestError, match="REST paginada"):
        value.get_review_data(42)
    assert "--paginate" in runner.calls[1] and "--slurp" in runner.calls[1]
