"""Testes do diagnóstico local do comando doctor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner, OutputPolicy
from ai_dev_orchestrator.services.doctor import (
    CheckScope,
    CheckStatus,
    DoctorCheck,
    DoctorService,
    has_errors,
)
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
from ai_dev_orchestrator.adapters.antigravity import AntigravityError
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA


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
        ("agy", "--help"): CommandResult(
            0,
            stderr=(
                "--input-format --sandbox --disable-slash-commands "
                "--output-format --json-schema --print-timeout --model\n"
            ),
        ),
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
    monkeypatch.setattr(shutil, "which", lambda command: None)
    result = CommandRunner().run(["missing", "--version"])

    assert result.returncode is None
    assert result.error == "Executável não encontrado: missing"


def test_command_runner_handles_failed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(["tool"], 2, b"", b"falhou")
    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", lambda *args, **kwargs: completed)

    result = CommandRunner().run(["tool"])

    assert result.returncode == 2
    assert result.stderr == "falhou"


def test_command_runner_handles_utf8_output_independently_of_system_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        ["tool"], 0, "emoji: 😀".encode(), "漢字".encode()
    )
    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", lambda *args, **kwargs: completed)

    result = CommandRunner().run(["tool"])

    assert result.succeeded
    assert result.stdout == "emoji: 😀"
    assert result.stderr == "漢字"


def test_command_runner_normalizes_utf8_decoding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args[0], 0, b"\x80", b"")

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner().run(["tool"], stdout_policy=OutputPolicy.UTF8_STRICT)

    assert result.returncode == 0
    assert not result.succeeded
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.error is not None
    assert "decodificar" in result.error
    assert "UTF-8" in result.error


def test_command_runner_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["tool"], 5)

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", timeout)
    result = CommandRunner().run(["tool"])

    assert result.returncode is None
    assert "timeout" in (result.error or "")


def test_command_runner_uses_safe_subprocess_options(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)
    CommandRunner(timeout=7).run(["tool", "--version"], input_text=None)

    assert received == {
        "capture_output": True,
        "timeout": 7,
        "shell": False,
        "check": False,
    }


def test_command_runner_forwards_textual_stdin_without_changing_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}
    arguments = ["tool", "exec", "-"]

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received["arguments"] = args[0]
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, b"stdout", b"stderr")

    monkeypatch.setattr(shutil, "which", lambda command: r"C:\\tools\\tool.exe")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner(timeout=7).run(arguments, cwd=tmp_path, input_text="texto")

    assert result.succeeded
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"
    assert arguments == ["tool", "exec", "-"]
    assert received["arguments"] == [r"C:\\tools\\tool.exe", "exec", "-"]
    assert received["input"] == b"texto"
    assert "text" not in received
    assert "encoding" not in received
    assert "errors" not in received
    assert received["shell"] is False
    assert received["timeout"] == 7
    assert received["capture_output"] is True
    assert received["cwd"] == tmp_path


def test_command_runner_preserves_unicode_stdin_bytes_without_newline_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = ("á漢😀" * 100_000) + "\n"
    expected_bytes = payload.encode("utf-8", errors="strict")
    received: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, kwargs["input"], b"")

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner().run(["tool", "exec", "-"], input_text=payload)

    assert received["input"] == expected_bytes
    assert len(received["input"]) == len(expected_bytes)
    assert hashlib.sha256(received["input"]).digest() == hashlib.sha256(expected_bytes).digest()
    assert received["input"].count(b"\n") == 1
    assert b"\r\n" not in received["input"]
    assert result.stdout == payload
    assert len(result.stdout) == len(payload)
    assert hashlib.sha256(result.stdout.encode("utf-8", errors="strict")).digest() == hashlib.sha256(expected_bytes).digest()


def test_command_runner_normalizes_utf8_input_encoding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner().run(["tool"], input_text="\ud800")

    assert result.returncode is None
    assert result.error is not None
    assert "codificar entrada textual" in result.error
    assert "UTF-8" in result.error


def test_command_runner_forwards_explicit_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    received: dict[str, object] = {}
    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", lambda *args, **kwargs: (received.update(kwargs), subprocess.CompletedProcess(args[0], 0, b"", b""))[1])
    CommandRunner().run(["tool"], cwd=tmp_path)
    assert received["cwd"] == tmp_path


def test_command_runner_resolves_path_executable_without_changing_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: dict[str, object] = {}
    resolved_commands: list[str] = []
    shim_path = r"C:\tools\bin\tool.CMD"
    arguments = ["tool", "exec", "--message", "texto com espaços"]

    def which(command: str) -> str:
        resolved_commands.append(command)
        return shim_path

    monkeypatch.setattr(shutil, "which", which)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received["arguments"] = args[0]
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "saída".encode(), "aviso".encode())

    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner(timeout=9).run(arguments, cwd=tmp_path)

    assert result.succeeded
    assert result.stdout == "saída"
    assert result.stderr == "aviso"
    assert resolved_commands == ["tool"]
    assert received["arguments"] == [shim_path, *arguments[1:]]
    assert arguments == ["tool", "exec", "--message", "texto com espaços"]
    assert received["cwd"] == tmp_path
    assert received["timeout"] == 9
    assert received["capture_output"] is True
    assert "text" not in received
    assert received["shell"] is False


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


def test_reports_antigravity_without_structured_output_capability(tmp_path) -> None:
    runner = FakeRunner(
        {
            ("agy", "--version"): CommandResult(0, "agy antigo"),
            ("agy", "--help"): CommandResult(0, "--input-format --sandbox"),
        }
    )

    check = DoctorService(runner, write_valid_config(tmp_path / "orchestrator.toml"))._check_antigravity_cli()

    assert check.status is CheckStatus.ERROR
    assert "--json-schema" in check.message
    assert "--output-format" in check.message


def test_accepts_antigravity_help_capabilities_from_stderr(tmp_path) -> None:
    runner = FakeRunner(
        {
            ("agy", "--version"): CommandResult(0, "agy 1.1.26"),
            ("agy", "--help"): CommandResult(
                0,
                stderr=(
                    "--input-format --sandbox --disable-slash-commands "
                    "--output-format --json-schema --print-timeout --model"
                ),
            ),
        }
    )

    check = DoctorService(runner, write_valid_config(tmp_path / "orchestrator.toml"))._check_antigravity_cli()

    assert check.status is CheckStatus.OK
    assert check.message.startswith("agy 1.1.26;")


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


def test_normal_doctor_does_not_run_deep_provider_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "version_info", SimpleNamespace(major=3, minor=13, micro=1))

    def forbidden(*args, **kwargs):
        raise AssertionError("provider não deve ser chamado sem --deep")

    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.CodexAdapter", forbidden)
    checks = DoctorService(
        FakeRunner(successful_results()), write_valid_config(tmp_path / "orchestrator.toml")
    ).diagnose()

    assert all(check.scope is CheckScope.LOCAL_CAPABILITY for check in checks)


def test_deep_provider_probes_use_a_discarded_synthetic_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspaces: list[Path] = []

    class Codex:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def execute(self, workspace, prompt):
            workspaces.append(Path(workspace))
            assert "ação" in prompt
            return SimpleNamespace(session_id="synthetic-session")

        def resume(self, workspace, session_id, prompt):
            assert Path(workspace) == workspaces[0]
            assert session_id == "synthetic-session"
            return SimpleNamespace(session_id=session_id)

    class Reviewer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def invoke(self, prompt, workspace, schema):
            assert Path(workspace) == workspaces[0]
            if schema is REVIEW_PLAN_SCHEMA:
                return json.dumps({field: [] for field in REVIEW_PLAN_SCHEMA["required"]})
            return json.dumps({
                "verdict": "APPROVED", "findings": [], "reviewed_head_sha": "0" * 40,
                "summary": "ação validada",
            })

    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.CodexAdapter", Codex)
    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.AntigravityAdapter", Reviewer)
    checks = DoctorService(config_path=write_valid_config(tmp_path / "orchestrator.toml"))._deep_provider_checks()

    assert all(check.status is CheckStatus.OK for check in checks)
    assert all(check.scope is CheckScope.LIVE_PROVIDER for check in checks)
    assert workspaces and not workspaces[0].exists()


def test_deep_structured_output_failure_is_a_provider_probe_failure(tmp_path: Path) -> None:
    class Reviewer:
        def invoke(self, prompt, workspace, schema):
            raise AntigravityError("Falha do contrato estruturado do reviewer")

    check = DoctorService()._probe_antigravity_schema(
        "Antigravity StructuredReview", Reviewer(), tmp_path, "probe",
        STRUCTURED_REVIEW_SCHEMA, lambda output: output,
    )

    assert check.status is CheckStatus.ERROR
    assert check.scope is CheckScope.LIVE_PROVIDER
    assert "contrato estruturado" in check.message


def test_deep_provider_quota_and_encoding_keep_their_specific_classification() -> None:
    service = DoctorService()
    quota = ProviderFailure(
        "codex", ProviderFailureKind.TERMINAL_QUOTA, "Limite de uso/quota esgotado",
        datetime.now(timezone.utc),
    )
    encoding = ProviderFailure(
        "gemini", ProviderFailureKind.ENCODING_ERROR, "Encoding UTF-8 inválido",
        datetime.now(timezone.utc),
    )

    assert service._provider_error("Codex exec", quota).message.startswith("TERMINAL_QUOTA")
    assert service._provider_error("Antigravity", encoding).message.startswith("ENCODING_ERROR")


def test_state_consistency_is_read_only_and_only_reports_divergence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    opened: list[bool] = []
    run = SimpleNamespace(
        issue_number=65, project_status="AI Review", pull_request_number=72,
        current_head_sha="a" * 40,
    )

    class Store:
        def __init__(self, path, *, read_only=False) -> None:
            opened.append(read_only)

        def list_active(self):
            return (run,)

    class Projects:
        def __init__(self, config) -> None:
            pass

        def list_items(self):
            return (SimpleNamespace(issue_number=65, status="Done"),)

    class PullRequests:
        def __init__(self, config) -> None:
            pass

        def get_merge_snapshot(self, number):
            return SimpleNamespace(head_sha="b" * 40)

    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.SqliteExecutionStore", Store)
    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.GitHubProjectAdapter", Projects)
    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.GitHubPullRequestAdapter", PullRequests)
    check = DoctorService(config_path=write_valid_config(tmp_path / "orchestrator.toml"))._check_state_consistency()

    assert opened == [True]
    assert check.status is CheckStatus.WARNING
    assert check.scope is CheckScope.STATE_CONSISTENCY
    assert "diverge" in check.message
