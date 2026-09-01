"""Testes unitários da atualização exclusiva de Status em GitHub Projects."""

from dataclasses import dataclass

import pytest

from ai_dev_orchestrator.adapters.github import (
    GitHubProjectStatusAdapter,
    GitHubProjectStatusError,
)
from ai_dev_orchestrator.config import GitHubConfig
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


@dataclass
class FakeRunner:
    """Executor determinístico que registra argumentos sem chamar o GitHub."""

    results: list[CommandResult]

    def __post_init__(self) -> None:
        self.arguments: list[list[str]] = []

    def run(self, arguments: list[str]) -> CommandResult:
        self.arguments.append(arguments)
        return self.results.pop(0)


def github_config() -> GitHubConfig:
    return GitHubConfig(
        owner="acme",
        repository="orchestrator",
        project_number=6,
        ready_status="Ready",
    )


def project_metadata() -> str:
    return '{"id":"PVT_kwDOB-exemplo"}'


def fields_payload() -> str:
    return (
        '{"fields":[{"id":"PVTF_prioridade","name":"Priority"},'
        '{"id":"PVTF_status","name":"Status","options":['
        '{"id":"option_ready","name":"Ready"},'
        '{"id":"option_progress","name":"In Progress"}]}]}'
    )


def successful_runner() -> FakeRunner:
    return FakeRunner(
        [
            CommandResult(0, project_metadata()),
            CommandResult(0, fields_payload()),
            CommandResult(0, '{"id":"ignored"}'),
        ]
    )


def test_sets_exact_status_with_ids_discovered_from_configured_project() -> None:
    runner = successful_runner()

    GitHubProjectStatusAdapter(github_config(), runner).set_status("PVT_item_9", "In Progress")

    assert runner.arguments == [
        ["gh", "project", "view", "6", "--owner", "acme", "--format", "json"],
        ["gh", "project", "field-list", "6", "--owner", "acme", "--format", "json"],
        [
            "gh", "project", "item-edit", "--id", "PVT_item_9", "--project-id",
            "PVT_kwDOB-exemplo", "--field-id", "PVTF_status",
            "--single-select-option-id", "option_progress",
        ],
    ]


def test_uses_remote_timeout_without_changing_default_process_timeout() -> None:
    adapter = GitHubProjectStatusAdapter(github_config())

    assert isinstance(adapter.runner, CommandRunner)
    assert adapter.runner.timeout == 20
    assert CommandRunner().timeout == 5


def test_rejects_missing_status_before_mutation() -> None:
    runner = FakeRunner([CommandResult(0, project_metadata()), CommandResult(0, fields_payload())])

    with pytest.raises(GitHubProjectStatusError, match="Status 'Done' não encontrado"):
        GitHubProjectStatusAdapter(github_config(), runner).set_status("PVT_item_9", "Done")

    assert len(runner.arguments) == 2
    assert all("item-edit" not in arguments for arguments in runner.arguments)


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([CommandResult(None, error="Executável não encontrado: gh")], "não encontrado"),
        ([CommandResult(None, error="Comando excedeu o timeout de 20s")], "timeout"),
        ([CommandResult(1, stderr="not authenticated")], "código 1.*not authenticated"),
        ([CommandResult(0, "invalid")], "JSON inválido"),
        ([CommandResult(0, "{}")], "campo 'id'"),
        ([CommandResult(0, project_metadata()), CommandResult(0, "{}")], "campo 'fields'"),
        (
            [CommandResult(0, project_metadata()), CommandResult(0, '{"fields":[]}')],
            "Campo Status 'Status' não encontrado",
        ),
        (
            [
                CommandResult(0, project_metadata()),
                CommandResult(0, '{"fields":[{"id":"field","name":"Status"}]}'),
            ],
            "campo 'options'",
        ),
    ],
)
def test_reports_discovery_failures(results: list[CommandResult], message: str) -> None:
    with pytest.raises(GitHubProjectStatusError, match=message):
        GitHubProjectStatusAdapter(github_config(), FakeRunner(results)).set_status("item", "Ready")


def test_reports_mutation_failure_and_never_adds_other_field_arguments() -> None:
    runner = successful_runner()
    runner.results[-1] = CommandResult(1, stderr="item not found")

    with pytest.raises(GitHubProjectStatusError, match="código 1.*item not found"):
        GitHubProjectStatusAdapter(github_config(), runner).set_status("PVT_item_9", "Ready")

    command = runner.arguments[-1]
    assert "--field-id" in command
    assert "PVTF_status" in command
    assert not set(command).intersection({"Priority", "Size", "Risk", "Agent"})
    assert "--text" not in command
    assert "--number" not in command
    assert "--clear" not in command


def test_uses_configured_status_field_name() -> None:
    config = github_config().model_copy(update={"status_field_name": "Workflow"})
    runner = FakeRunner(
        [
            CommandResult(0, project_metadata()),
            CommandResult(
                0,
                '{"fields":[{"id":"workflow","name":"Workflow","options":['
                '{"id":"ready","name":"Ready"}]}]}',
            ),
            CommandResult(0),
        ]
    )

    GitHubProjectStatusAdapter(config, runner).set_status("item", "Ready")

    assert runner.arguments[-1][-1] == "ready"
