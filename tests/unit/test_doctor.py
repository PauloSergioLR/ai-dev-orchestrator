"""Testes do diagnóstico local do comando doctor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.services.doctor import (
    CheckStatus,
    DoctorCheck,
    DoctorService,
    has_errors,
)


@dataclass
class FakeRunner:
    results: dict[tuple[str, ...], CommandResult]

    def run(self, arguments: list[str]) -> CommandResult:
        return self.results[tuple(arguments)]


def successful_results() -> dict[tuple[str, ...], CommandResult]:
    return {
        ("git", "--version"): CommandResult(0, "git version 2.50.0\n"),
        ("gh", "auth", "status"): CommandResult(0),
        ("codex", "--version"): CommandResult(0, "codex 1.0\n"),
        ("agy", "--version"): CommandResult(0, "agy 1.0\n"),
        ("git", "rev-parse", "--is-inside-work-tree"): CommandResult(0, "true\n"),
        ("git", "remote"): CommandResult(0, "origin\n"),
    }


def write_valid_config(path: Path) -> Path:
    repository = (path.parent / "repository").as_posix()
    worktrees = (path.parent / "worktrees").as_posix()
    path.write_text(
        "[github]\nowner = 'a'\nrepository = 'b'\nproject_number = 1\nready_status = 'Ready'\n"
        f"[workspace]\nrepository_path = '{repository}'\nworktrees_dir = '{worktrees}'\nbase_ref = 'main'\n"
        "[execution]\nmax_attempts = 1\nmax_parallel_runs = 1\nauto_merge = false\n",
        encoding="utf-8",
    )
    return path


def test_all_checks_are_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "version_info", SimpleNamespace(major=3, minor=13, micro=1))

    checks = DoctorService(
        FakeRunner(successful_results()), write_valid_config(tmp_path / "orchestrator.toml")
    ).diagnose()

    assert all(check.status is CheckStatus.OK for check in checks)


def test_command_runner_handles_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    result = CommandRunner().run(["missing", "--version"])

    assert result.returncode is None
    assert result.error == "Executável não encontrado: missing"


def test_command_runner_handles_failed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(["tool"], 2, "", "falhou")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    result = CommandRunner().run(["tool"])

    assert result.returncode == 2
    assert result.stderr == "falhou"


def test_command_runner_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["tool"], 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = CommandRunner().run(["tool"])

    assert result.returncode is None
    assert "timeout" in (result.error or "")


def test_command_runner_uses_safe_subprocess_options(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    CommandRunner(timeout=7).run(["tool", "--version"])

    assert received == {
        "capture_output": True,
        "text": True,
        "timeout": 7,
        "shell": False,
        "check": False,
    }


def test_command_runner_forwards_explicit_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    received: dict[str, object] = {}
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (received.update(kwargs), subprocess.CompletedProcess(args[0], 0, "", ""))[1])
    CommandRunner().run(["tool"], cwd=tmp_path)
    assert received["cwd"] == tmp_path


def test_reports_incompatible_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", SimpleNamespace(major=3, minor=12, micro=9))

    check = DoctorService()._check_python()

    assert check.status is CheckStatus.ERROR
    assert "3.13.x" in check.message


def test_reports_unauthenticated_github_cli() -> None:
    runner = FakeRunner({("gh", "auth", "status"): CommandResult(1, stderr="not logged in")})

    check = DoctorService(runner)._check_github_cli()

    assert check.status is CheckStatus.ERROR
    assert "não está autenticado" in check.message


def test_reports_non_git_directory() -> None:
    runner = FakeRunner({("git", "rev-parse", "--is-inside-work-tree"): CommandResult(128)})

    check = DoctorService(runner)._check_repository()

    assert check.status is CheckStatus.ERROR
    assert "não é um repositório Git" in check.message


def test_reports_missing_git_remote() -> None:
    runner = FakeRunner(
        {
            ("git", "rev-parse", "--is-inside-work-tree"): CommandResult(0, "true"),
            ("git", "remote"): CommandResult(0),
        }
    )

    check = DoctorService(runner)._check_repository()

    assert check.status is CheckStatus.ERROR
    assert "nenhum remote" in check.message


def test_reports_missing_configuration(tmp_path: Path) -> None:
    check = DoctorService(config_path=tmp_path / "missing.toml")._check_configuration()

    assert check.status is CheckStatus.ERROR
    assert "não encontrado" in check.message


def test_reports_invalid_configuration(tmp_path: Path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text("[github", encoding="utf-8")

    check = DoctorService(config_path=config)._check_configuration()

    assert check.status is CheckStatus.ERROR
    assert "TOML inválido" in check.message


def test_error_results_produce_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_dev_orchestrator.cli.DoctorService.diagnose",
        lambda self: [DoctorCheck("Git", CheckStatus.ERROR, "indisponível")],
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "ERROR" in result.output


def test_ok_and_warning_results_produce_zero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_dev_orchestrator.cli.DoctorService.diagnose",
        lambda self: [
            DoctorCheck("Git", CheckStatus.OK, "disponível"),
            DoctorCheck("Optional", CheckStatus.WARNING, "atenção"),
        ],
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "WARNING" in result.output
    assert not has_errors([DoctorCheck("Optional", CheckStatus.WARNING, "atenção")])


def test_doctor_appears_in_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.output
