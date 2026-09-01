"""Testes unitários dos gates e da publicação sem processos reais."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.publication import GitPublicationAdapter, GitPublicationError
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.validation import LocalValidationError, LocalValidationService


@dataclass
class FakeRunner:
    results: list[CommandResult]
    calls: list[tuple[tuple[str, ...], Path | None]] = field(default_factory=list)

    def run(self, arguments: tuple[str, ...], cwd: Path | None = None) -> CommandResult:
        self.calls.append((tuple(arguments), cwd))
        return self.results.pop(0)


def test_gates_run_in_worktree_in_required_order() -> None:
    worktree = Path("C:/worktrees/issue")
    runner = FakeRunner([CommandResult(0), CommandResult(0), CommandResult(0)])

    results = LocalValidationService(runner).validate(worktree)

    assert [result.name for result in results] == ["ruff", "pytest", "diff_check"]
    assert [call[0] for call in runner.calls] == [
        ("uv", "run", "ruff", "check", "."), ("uv", "run", "pytest"), ("git", "diff", "--check"),
    ]
    assert all(cwd == worktree for _, cwd in runner.calls)


def test_failed_gate_is_fail_fast_and_keeps_diagnostic() -> None:
    runner = FakeRunner([CommandResult(1, stderr="erro do ruff")])

    with pytest.raises(LocalValidationError, match="ruff.*erro do ruff"):
        LocalValidationService(runner).validate(Path("C:/worktree"))

    assert len(runner.calls) == 1


def test_truncates_large_gate_diagnostic() -> None:
    runner = FakeRunner([CommandResult(1, stderr="x" * 1000)])

    with pytest.raises(LocalValidationError, match="saída truncada"):
        LocalValidationService(runner).validate(Path("C:/worktree"))


def test_publication_stages_validates_commits_and_pushes_without_force() -> None:
    runner = FakeRunner([CommandResult(0, " M arquivo.py\n"), CommandResult(0), CommandResult(0), CommandResult(0), CommandResult(0, "abc123\n"), CommandResult(0)])
    adapter = GitPublicationAdapter(runner)

    assert adapter.commit("C:/worktree", 19) == "abc123"
    adapter.push("C:/worktree", "upstream", "feat/publish")

    commands = [call[0] for call in runner.calls]
    assert commands == [
        ("git", "status", "--porcelain", "--untracked-files=all"), ("git", "add", "-A"),
        ("git", "diff", "--cached", "--check"), ("git", "commit", "-m", "feat: implementa issue #19"),
        ("git", "rev-parse", "HEAD"), ("git", "push", "-u", "upstream", "feat/publish"),
    ]
    assert all("--force" not in command for command in commands)


def test_no_changes_stops_before_stage_or_commit() -> None:
    runner = FakeRunner([CommandResult(0)])
    with pytest.raises(GitPublicationError, match="Não há alterações"):
        GitPublicationAdapter(runner).commit("C:/worktree", 19)
    assert len(runner.calls) == 1


def test_commit_failure_does_not_attempt_push() -> None:
    runner = FakeRunner([CommandResult(0, " M arquivo.py\n"), CommandResult(0), CommandResult(0), CommandResult(1, stderr="identidade ausente")])
    adapter = GitPublicationAdapter(runner)
    with pytest.raises(GitPublicationError, match="identidade"):
        adapter.commit("C:/worktree", 19)
    assert all(command[0:2] != ("git", "push") for command, _ in runner.calls)
