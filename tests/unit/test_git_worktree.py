"""Testes unitários do isolamento local por Git worktree."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter, GitWorktreeError
from ai_dev_orchestrator.infrastructure.process import CommandResult


@dataclass
class FakeRunner:
    results: list[CommandResult]
    arguments: list[list[str]] = field(default_factory=list)

    def run(self, arguments: list[str]) -> CommandResult:
        self.arguments.append(arguments)
        return self.results.pop(0)


def repository_result(root: Path) -> CommandResult:
    return CommandResult(0, f"{root}\n")


def valid_creation_results(root: Path) -> list[CommandResult]:
    return [
        repository_result(root),
        CommandResult(0),
        CommandResult(1),
        CommandResult(0, "commit-id\n"),
        CommandResult(0),
    ]


def test_validates_repository_and_returns_git_root(tmp_path: Path) -> None:
    repository = tmp_path / "input"
    root = tmp_path / "repository"
    runner = FakeRunner([repository_result(root)])

    assert GitWorktreeAdapter(runner).validate_repository(repository) == root
    assert runner.arguments == [
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"]
    ]


def test_creates_worktree_with_list_arguments_and_returns_model(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    destination = tmp_path / "isolated"
    runner = FakeRunner(valid_creation_results(repository))

    worktree = GitWorktreeAdapter(runner).create_worktree(
        repository, "feat/isolated", destination, "main"
    )

    assert worktree.repository_root == repository
    assert worktree.path == destination
    assert worktree.branch == "feat/isolated"
    assert worktree.base_ref == "main"
    assert runner.arguments == [
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        ["git", "-C", str(repository), "check-ref-format", "--branch", "feat/isolated"],
        [
            "git", "-C", str(repository), "show-ref", "--verify", "--quiet",
            "refs/heads/feat/isolated",
        ],
        ["git", "-C", str(repository), "rev-parse", "--verify", "main^{commit}"],
        [
            "git", "-C", str(repository), "worktree", "add", "-b", "feat/isolated",
            str(destination), "main",
        ],
    ]
    assert all(isinstance(arguments, list) for arguments in runner.arguments)
    assert "--force" not in [argument for command in runner.arguments for argument in command]


def test_resolves_relative_worktree_path_from_repository_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner(valid_creation_results(repository))

    worktree = GitWorktreeAdapter(runner).create_worktree(
        repository, "feat/isolated", "worktrees/isolated", "main"
    )

    assert worktree.path == repository / "worktrees" / "isolated"
    assert runner.arguments[-1][-2] == str(worktree.path)


def test_rejects_invalid_branch_before_checking_existing_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner([repository_result(repository), CommandResult(1, stderr="invalid ref")])

    with pytest.raises(GitWorktreeError, match="branch é inválido"):
        GitWorktreeAdapter(runner).create_worktree(
            repository, "bad..branch", tmp_path / "isolated", "main"
        )

    assert len(runner.arguments) == 2


def test_rejects_existing_local_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner(
        [repository_result(repository), CommandResult(0), CommandResult(0)]
    )

    with pytest.raises(GitWorktreeError, match="branch local já existe"):
        GitWorktreeAdapter(runner).create_worktree(
            repository, "feat/isolated", tmp_path / "isolated", "main"
        )

    assert len(runner.arguments) == 3


def test_rejects_existing_destination_without_calling_worktree_add(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    destination = tmp_path / "isolated"
    destination.mkdir()
    runner = FakeRunner(
        [repository_result(repository), CommandResult(0), CommandResult(1)]
    )

    with pytest.raises(GitWorktreeError, match="destino.*já existe"):
        GitWorktreeAdapter(runner).create_worktree(
            repository, "feat/isolated", destination, "main"
        )

    assert all("worktree" not in command for command in runner.arguments)


def test_rejects_unresolvable_base_ref(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner(
        [
            repository_result(repository),
            CommandResult(0),
            CommandResult(1),
            CommandResult(128, stderr="unknown revision"),
        ]
    )

    with pytest.raises(GitWorktreeError, match="resolver a referência base.*unknown revision"):
        GitWorktreeAdapter(runner).create_worktree(
            repository, "feat/isolated", tmp_path / "isolated", "missing"
        )


def test_reports_missing_git_executable(tmp_path: Path) -> None:
    runner = FakeRunner([CommandResult(None, error="Executável não encontrado: git")])

    with pytest.raises(GitWorktreeError, match="executar Git.*não encontrado"):
        GitWorktreeAdapter(runner).validate_repository(tmp_path / "repository")


def test_reports_invalid_repository_and_nonzero_git_result(tmp_path: Path) -> None:
    runner = FakeRunner([CommandResult(128, stderr="not a git repository")])

    with pytest.raises(GitWorktreeError, match="código 128.*not a git repository"):
        GitWorktreeAdapter(runner).validate_repository(tmp_path / "not-a-repository")


def test_removes_worktree_without_force_or_branch_deletion(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    destination = tmp_path / "isolated"
    runner = FakeRunner([repository_result(repository), CommandResult(0)])

    GitWorktreeAdapter(runner).remove_worktree(repository, destination)

    assert runner.arguments[-1] == [
        "git", "-C", str(repository), "worktree", "remove", str(destination)
    ]
    all_arguments = [argument for command in runner.arguments for argument in command]
    assert "--force" not in all_arguments
    assert "branch" not in all_arguments


def test_reports_refused_removal_without_destroying_work(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner(
        [repository_result(repository), CommandResult(128, stderr="contains modified files")]
    )

    with pytest.raises(GitWorktreeError, match="remover o worktree.*modified files"):
        GitWorktreeAdapter(runner).remove_worktree(repository, tmp_path / "isolated")

    assert runner.arguments[-1][3:] == ["worktree", "remove", str(tmp_path / "isolated")]


def test_prepares_remote_base_and_checks_remote_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner([
        repository_result(repository), CommandResult(0), CommandResult(1),
        CommandResult(0), CommandResult(0, "commit-id\n"), CommandResult(2),
    ])

    base = GitWorktreeAdapter(runner).prepare_remote_base(
        repository, "origin", "origin/main", "work/nova",
    )

    assert base == "refs/remotes/origin/main"
    assert runner.arguments[3][-2:] == [
        "origin", "refs/heads/main:refs/remotes/origin/main",
    ]
    assert runner.arguments[-1][-2:] == ["origin", "refs/heads/work/nova"]
    assert all("--force" not in command for command in runner.arguments)


def test_remote_branch_collision_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = FakeRunner([
        repository_result(repository), CommandResult(0), CommandResult(1),
        CommandResult(0), CommandResult(0, "commit-id\n"), CommandResult(0, "sha\tref\n"),
    ])

    with pytest.raises(GitWorktreeError, match="branch remota já existe"):
        GitWorktreeAdapter(runner).prepare_remote_base(
            repository, "origin", "main", "work/existente",
        )
