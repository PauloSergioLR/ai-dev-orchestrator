"""Testes unitários dos gates e da publicação sem processos reais."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess

import pytest

from ai_dev_orchestrator.adapters.publication import GitPublicationAdapter, GitPublicationError
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.validation import (
    LocalFailureKind,
    LocalValidationError,
    LocalValidationService,
)
from ai_dev_orchestrator.domain.project_contract import CommandPlan


PLANS = (
    CommandPlan("lint", "lint", "Lint", ("quality", "--lint")),
    CommandPlan("unit", "unit", "Unit", ("quality", "--unit")),
    CommandPlan("diff_check", "format/check", "Diff", ("quality", "--diff")),
)


@dataclass
class FakeRunner:
    results: list[CommandResult]
    calls: list[tuple[tuple[str, ...], Path | None]] = field(default_factory=list)

    def run(
        self, arguments: tuple[str, ...], cwd: Path | None = None, **_kwargs
    ) -> CommandResult:
        self.calls.append((tuple(arguments), cwd))
        return self.results.pop(0)


def test_gates_run_in_worktree_in_required_order(tmp_path: Path) -> None:
    worktree = tmp_path / "issue"
    runner = FakeRunner([CommandResult(0), CommandResult(0), CommandResult(0)])

    worktree.mkdir(parents=True)
    results = LocalValidationService(runner, PLANS).validate(worktree)

    assert [result.name for result in results] == ["lint", "unit", "diff_check"]
    assert [call[0] for call in runner.calls] == [
        ("quality", "--lint"), ("quality", "--unit"), ("quality", "--diff"),
    ]
    assert all(cwd == worktree for _, cwd in runner.calls)


def test_failed_gate_is_fail_fast_and_keeps_diagnostic() -> None:
    runner = FakeRunner([CommandResult(1, stderr="erro do ruff")])

    worktree = Path.cwd()
    with pytest.raises(LocalValidationError, match="lint.*erro do ruff"):
        LocalValidationService(runner, PLANS).validate(worktree)

    assert len(runner.calls) == 1


def test_uv_gate_does_not_inherit_virtualenv_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: dict[str, object] = {}
    foreign_environment = tmp_path.parent / "checkout-principal" / ".venv"

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received["command"] = args[0]
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, b"teste aprovado", b"")

    monkeypatch.setenv("VIRTUAL_ENV", str(foreign_environment))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path.parent / "checkout-principal"))
    monkeypatch.setenv("PATH", "caminho-preservado")
    monkeypatch.setenv("PROJECT_AUTH_TOKEN", "preservar")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.resolve_executable", lambda command, *args: command)
    monkeypatch.setattr(
        "ai_dev_orchestrator.infrastructure.process.run_captured", run
    )
    plan = CommandPlan(
        "executar-os-testes", "unit", "Testes", ("uv", "run", "pytest", "-q")
    )

    result = LocalValidationService(plans=(plan,)).validate(tmp_path)

    assert result[0].succeeded
    assert "VIRTUAL_ENV" not in received["env"]
    assert "PYTHONPATH" not in received["env"]
    assert received["env"]["PATH"] == "caminho-preservado"
    assert received["env"]["PROJECT_AUTH_TOKEN"] == "preservar"
    assert received["command"] == ["uv", "run", "pytest", "-q"]
    assert os.environ["VIRTUAL_ENV"] == str(foreign_environment)
    assert os.environ["PYTHONPATH"] == str(tmp_path.parent / "checkout-principal")


def test_uv_gate_with_active_preserves_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "ambiente-ativo")

    environment = LocalValidationService._gate_environment(
        ("uv", "run", "--active", "pytest", "-q")
    )

    assert environment["VIRTUAL_ENV"] == "ambiente-ativo"
    assert os.environ["VIRTUAL_ENV"] == "ambiente-ativo"


def test_pytest_gate_isolates_external_options_cache_and_temporary_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inherited_cache = tmp_path / ".pytest_cache"
    inherited_cache.mkdir()
    marker = inherited_cache / "estado-antigo"
    marker.write_text("não remover", encoding="utf-8")
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--cache-dir={inherited_cache} --invalid-option")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path / "temporario-antigo"))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp-antigo"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp-antigo"))
    received: dict[str, str] = {}
    isolated_root: Path | None = None

    class Runner:
        def run(self, _arguments, cwd=None, *, environment=None):
            nonlocal isolated_root
            assert cwd == tmp_path
            received.update(environment)
            isolated_root = Path(environment["PYTEST_DEBUG_TEMPROOT"])
            assert isolated_root.is_dir()
            if str(inherited_cache) in environment["PYTEST_ADDOPTS"]:
                return CommandResult(
                    1, stderr=f"PermissionError: Access is denied: '{inherited_cache}'"
                )
            return CommandResult(0)

    plan = CommandPlan("testes", "unit", "Testes", ("uv", "run", "pytest", "-q"))

    result = LocalValidationService(Runner(), (plan,)).validate(tmp_path)

    assert result[0].succeeded
    assert isolated_root is not None and not isolated_root.exists()
    assert marker.read_text(encoding="utf-8") == "não remover"
    assert not list(tmp_path.glob(".orch-gate-*"))
    assert os.environ["PYTEST_ADDOPTS"].endswith("--invalid-option")
    assert os.environ["PYTEST_DEBUG_TEMPROOT"] == str(
        tmp_path / "temporario-antigo"
    )


@pytest.mark.parametrize(
    ("platform", "isolated_names", "unchanged_names"),
    [
        ("nt", ("TMP", "TEMP", "TMPDIR"), ()),
        ("posix", ("TMPDIR",), ("TMP", "TEMP")),
    ],
)
def test_pytest_temporary_environment_is_platform_specific(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    isolated_names: tuple[str, ...],
    unchanged_names: tuple[str, ...],
) -> None:
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, f"original-{name}")

    environment = LocalValidationService._gate_environment(
        ("python", "-m", "pytest"), tmp_path, platform=platform
    )

    for name in isolated_names:
        assert environment[name] == str(tmp_path)
    for name in unchanged_names:
        assert environment[name] == f"original-{name}"


def test_non_python_gate_preserves_generic_environment_without_pytest_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setenv("PATH", "caminho-das-ferramentas")
    monkeypatch.setenv("PROJECT_AUTH_TOKEN", "credencial-do-projeto")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--opcao-externa")
    received: dict[str, str] = {}

    class Runner:
        def run(self, _arguments, cwd=None, *, environment=None):
            received.update(environment)
            return CommandResult(0)

    plan = CommandPlan("frontend", "test", "Frontend", ("npm", "test"))

    LocalValidationService(Runner(), (plan,)).validate(tmp_path)

    assert received["PATH"] == "caminho-das-ferramentas"
    assert received["PROJECT_AUTH_TOKEN"] == "credencial-do-projeto"
    assert received["PYTEST_ADDOPTS"] == "--opcao-externa"
    assert "PYTEST_DEBUG_TEMPROOT" not in received
    assert not list(tmp_path.glob(".orch-gate-*"))


@pytest.mark.parametrize("failure", [
    "PermissionError: Access is denied", "PermissionError",
    "OSError: [WinError 32] File in use",
])
def test_access_denied_in_controlled_temporary_is_infrastructure_failure(
    tmp_path: Path, failure: str,
) -> None:
    class Runner:
        def run(self, _arguments, cwd=None, *, environment=None):
            root = environment["PYTEST_DEBUG_TEMPROOT"]
            return CommandResult(
                1,
                stderr="x" * 600 + f" {failure}: '{root}'",
            )

    plan = CommandPlan("testes", "unit", "Testes", ("pytest", "-q"))

    with pytest.raises(LocalValidationError) as raised:
        LocalValidationService(Runner(), (plan,)).validate(tmp_path)

    assert raised.value.kind is LocalFailureKind.LOCAL_INFRASTRUCTURE
    assert raised.value.correctable is False
    assert raised.value.result is not None
    assert "saída truncada" in raised.value.result.diagnostic
    assert not list(tmp_path.glob(".orch-gate-*"))


def test_truncates_large_gate_diagnostic() -> None:
    runner = FakeRunner([CommandResult(1, stderr="x" * 1000)])

    with pytest.raises(LocalValidationError, match="saída truncada"):
        LocalValidationService(runner, PLANS).validate(Path.cwd())


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


def test_correction_commit_uses_fixed_message_without_rewriting_history() -> None:
    runner = FakeRunner([CommandResult(0, " M arquivo.py\n"), CommandResult(0), CommandResult(0), CommandResult(0), CommandResult(0, "def456\n")])

    assert GitPublicationAdapter(runner).commit_correction("C:/worktree") == "def456"

    commands = [call[0] for call in runner.calls]
    assert ("git", "commit", "-m", "fix: corrige findings do reviewer") in commands
    assert all("--force" not in command and "reset" not in command and "rebase" not in command for command in commands)


def test_reads_current_head_without_mutating_worktree() -> None:
    runner = FakeRunner([CommandResult(0, "abc123\n")])

    assert GitPublicationAdapter(runner).current_head("C:/worktree") == "abc123"
    assert runner.calls == [(("git", "rev-parse", "HEAD"), "C:/worktree")]


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
