"""Testes do diagnóstico local do comando doctor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner, OutputPolicy
from ai_dev_orchestrator.infrastructure.codex_runtime import CodexCandidate
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

    def run(self, arguments: list[str], **policies) -> CommandResult:
        if arguments[:1] == ["git"] and "get-url" in arguments:
            return CommandResult(0, "https://github.com/a/b.git")
        return self.results[tuple(arguments)]


def successful_results() -> dict[tuple[str, ...], CommandResult]:
    return {
        ("gh", "project", "field-list", "1", "--owner", "a", "--format", "json"): CommandResult(0, json.dumps({"fields": [{"id": "status", "name": "Status", "options": [{"id": str(index), "name": name} for index, name in enumerate(("Ready", "In Progress", "AI Review", "Done"))]}]})),
        ("git", "--version"): CommandResult(0, "git version 2.50.0\n"),
        ("gh", "auth", "status"): CommandResult(0),
        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(0, '{"items":[]}'),
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
    repository_path = path.parent / "repository"
    repository_path.mkdir(exist_ok=True)
    repository = repository_path.as_posix()
    worktrees = (path.parent / "worktrees").as_posix()
    state = (path.parent / "state" / "orchestrator.db").as_posix()
    path.write_text(
        "[github]\nowner = 'a'\nrepository = 'b'\nproject_number = 1\nready_status = 'Ready'\n"
        f"[workspace]\nrepository_path = '{repository}'\nworktrees_dir = '{worktrees}'\nbase_ref = 'main'\n"
        "[execution]\nmax_attempts = 1\nmax_parallel_runs = 1\nauto_merge = false\n"
        f"[state]\ndatabase_path = '{state}'\n",
        encoding="utf-8",
    )
    return path


def test_all_checks_are_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.sys",
        SimpleNamespace(version_info=SimpleNamespace(major=3, minor=13, micro=1)),
    )
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    executable = tmp_path / "repository" / "tools" / "validate.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    config_path = write_valid_config(tmp_path / "orchestrator.toml")
    with config_path.open("a", encoding="utf-8") as config:
        config.write(
            "[project]\n[[project.gates]]\n"
            "name = 'official-check'\nargv = ['tools/validate.exe']\n"
        )
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.codex_candidates",
        lambda: (CodexCandidate(str(tmp_path / "codex"), "fixture"),),
    )

    checks = DoctorService(
        FakeRunner(successful_results()), config_path
    ).diagnose()

    assert all(check.status is CheckStatus.OK for check in checks), [
        (check.name, check.status, check.message)
        for check in checks
        if check.status is not CheckStatus.OK
    ]


def test_command_runner_handles_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: None)
    result = CommandRunner().run(["missing", "--version"])

    assert result.returncode is None
    assert result.error == "Executável não encontrado: missing"


def test_command_runner_handles_failed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(["tool"], 2, b"", b"falhou")
    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
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
    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
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

    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
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

    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", timeout)
    result = CommandRunner().run(["tool"])

    assert result.returncode is None
    assert "timeout" in (result.error or "")


def test_command_runner_uses_safe_subprocess_options(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
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

    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.resolve_executable", lambda command, *args: r"C:\\tools\\tool.exe")
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

    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
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

    monkeypatch.setattr(shutil, "which", lambda command, **kwargs: command)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner().run(["tool"], input_text="\ud800")

    assert result.returncode is None
    assert result.error is not None
    assert "codificar entrada textual" in result.error
    assert "UTF-8" in result.error


def test_command_runner_forwards_explicit_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    received: dict[str, object] = {}
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.resolve_executable", lambda command, *args: command)
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

    def which(command: str, *args, **kwargs) -> str:
        resolved_commands.append(command)
        return shim_path

    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.resolve_executable", which)

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
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.sys",
        SimpleNamespace(version_info=SimpleNamespace(major=3, minor=12, micro=9)),
    )

    check = DoctorService()._check_python()

    assert check.status is CheckStatus.ERROR
    assert "3.13.x" in check.message


def test_doctor_confirms_read_only_github_project_access(tmp_path: Path) -> None:
    runner = FakeRunner({
        ("gh", "project", "field-list", "1", "--owner", "a", "--format", "json"): CommandResult(0, json.dumps({"fields": [{"id": "status", "name": "Status", "options": [{"id": str(index), "name": name} for index, name in enumerate(("Ready", "In Progress", "AI Review", "Done"))]}]})),

        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(0, '{"items":[]}'),
    })

    check = DoctorService(
        runner, write_valid_config(tmp_path / "orchestrator.toml")
    )._check_github_project()

    assert check.status is CheckStatus.OK
    assert "Project 1" in check.message
    assert "timeout=60s" in check.message


def test_doctor_reports_project_timeout_with_configurable_action(tmp_path: Path) -> None:
    runner = FakeRunner({
        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(None, error="Comando excedeu o timeout de 60s"),
    })

    check = DoctorService(
        runner, write_valid_config(tmp_path / "orchestrator.toml")
    )._check_github_project()

    assert check.status is CheckStatus.ERROR
    assert "timeout" in check.message.casefold()
    assert "github.project_timeout_seconds" in check.message


def test_doctor_reports_unknown_owner_with_project_scope_action(tmp_path: Path) -> None:
    runner = FakeRunner({
        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(1, stderr="unknown owner type"),
    })

    check = DoctorService(
        runner, write_valid_config(tmp_path / "orchestrator.toml")
    )._check_github_project()

    assert check.status is CheckStatus.ERROR
    assert "owner não reconhecido" in check.message
    assert "gh auth refresh -h github.com -s project" in check.message


def test_doctor_reports_project_not_found_separately(tmp_path: Path) -> None:
    runner = FakeRunner({
        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(1, stderr="Project not found"),
    })

    check = DoctorService(
        runner, write_valid_config(tmp_path / "orchestrator.toml")
    )._check_github_project()

    assert check.status is CheckStatus.ERROR
    assert "github.owner" in check.message
    assert "github.project_number" in check.message
    assert "auth refresh" not in check.message


def test_doctor_identifies_environment_token_override_without_leaking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "segredo-do-teste")
    runner = FakeRunner({
        (
            "gh", "project", "item-list", "1", "--owner", "a", "--limit",
            "1000", "--format", "json",
        ): CommandResult(1, stderr="HTTP 403: insufficient scopes"),
    })

    check = DoctorService(
        runner, write_valid_config(tmp_path / "orchestrator.toml")
    )._check_github_project()

    assert check.status is CheckStatus.ERROR
    assert "GH_TOKEN" in check.message
    assert "segredo-do-teste" not in check.message


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


def test_reports_non_git_directory(tmp_path: Path) -> None:
    runner = FakeRunner({("git", "rev-parse", "--is-inside-work-tree"): CommandResult(128)})

    check = DoctorService(runner, write_valid_config(tmp_path / "orchestrator.toml"))._check_repository()

    assert check.status is CheckStatus.ERROR
    assert "não é um repositório Git" in check.message


def test_reports_missing_git_remote(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("git", "rev-parse", "--is-inside-work-tree"): CommandResult(0, "true"),
            ("git", "remote"): CommandResult(0),
        }
    )

    check = DoctorService(runner, write_valid_config(tmp_path / "orchestrator.toml"))._check_repository()

    assert check.status is CheckStatus.ERROR
    assert "remote configurado não existe" in check.message


def test_reports_missing_configuration(tmp_path: Path) -> None:
    check = DoctorService(config_path=tmp_path / "missing.toml")._check_configuration()

    assert check.status is CheckStatus.ERROR
    assert "não encontrado" in check.message


def test_environment_probe_still_runs_without_project_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    checks = DoctorService(
        config_path=tmp_path / "missing.toml"
    )._check_local_permissions()

    by_name = {check.name: check for check in checks}
    assert by_name["Configured path probes"].status is CheckStatus.ERROR
    assert by_name["Temporary directory"].status is CheckStatus.OK


def test_write_probe_validates_writable_directory_and_removes_only_its_artifact(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "arquivo-do-usuario.txt"
    existing.write_text("preservar", encoding="utf-8")

    check = DoctorService()._probe_directory_write(
        "Workspace write probe", tmp_path, must_exist=True
    )

    assert check.status is CheckStatus.OK
    assert existing.read_text(encoding="utf-8") == "preservar"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("message", ["Access is denied", "Acesso negado"])
def test_write_probe_reports_sanitized_access_denied_during_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, message: str
) -> None:
    def deny_creation(*args: object, **kwargs: object) -> str:
        raise OSError(message)

    monkeypatch.setattr(tempfile, "mkdtemp", deny_creation)

    check = DoctorService()._probe_directory_write(
        "Worktrees write probe", tmp_path, must_exist=True
    )

    assert check.status is CheckStatus.ERROR
    assert "criar diretório temporário exclusivo" in check.message
    assert "acesso negado" in check.message
    assert str(tmp_path) in check.message
    assert "impede uma execução normal" in check.message


def test_write_probe_reports_failure_to_create_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_write_text = Path.write_text

    def deny_file_creation(path: Path, *args: object, **kwargs: object) -> int:
        if path.name == "write-probe.txt":
            raise PermissionError("Acesso negado")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", deny_file_creation)

    check = DoctorService()._probe_directory_write(
        "Workspace write probe", tmp_path, must_exist=True
    )

    assert check.status is CheckStatus.ERROR
    assert "criar/escrever arquivo" in check.message
    assert "acesso negado" in check.message
    assert not any(path.name.startswith(".orch-doctor-") for path in tmp_path.iterdir())


def test_write_probe_reports_cleanup_failure_without_removing_user_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = tmp_path / "preservar.txt"
    existing.write_text("usuário", encoding="utf-8")
    original_unlink = Path.unlink

    def deny_probe_removal(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "write-probe.txt":
            raise PermissionError("Access is denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_probe_removal)

    check = DoctorService()._probe_directory_write(
        "State directory", tmp_path, must_exist=True
    )

    assert check.status is CheckStatus.ERROR
    assert "remover arquivo temporário" in check.message
    assert "acesso negado" in check.message
    assert existing.read_text(encoding="utf-8") == "usuário"


def test_write_probe_reports_required_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "workspace-ausente"

    check = DoctorService()._probe_directory_write(
        "Workspace write probe", missing, must_exist=True
    )

    assert check.status is CheckStatus.ERROR
    assert str(missing) in check.message
    assert "diretório obrigatório inexistente" in check.message
    assert not missing.exists()


def test_local_permissions_probe_workspace_and_separate_worktrees_directory(
    tmp_path: Path,
) -> None:
    config_path = write_valid_config(tmp_path / "orchestrator.toml")
    workspace = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"

    checks = DoctorService(config_path=config_path)._check_local_permissions()
    by_name = {check.name: check for check in checks}

    assert str(workspace) in by_name["Workspace write probe"].message
    assert str(worktrees) in by_name["Worktrees write probe"].message
    assert all(check.status is CheckStatus.OK for check in checks), [
        (check.name, check.status, check.message) for check in checks
    ]
    assert not worktrees.exists()


def test_uv_cache_is_probed_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uv_cache = tmp_path / "uv-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(uv_cache))

    checks = DoctorService(
        config_path=write_valid_config(tmp_path / "orchestrator.toml")
    )._check_local_permissions()

    uv_check = next(check for check in checks if check.name == "uv cache write probe")
    assert uv_check.status is CheckStatus.OK
    assert str(uv_cache) in uv_check.message
    assert not uv_cache.exists()


class CrgRunner:
    def __init__(self, results):
        self.results = results

    def run(self, arguments, cwd=None, **kwargs):
        return self.results.get(tuple(arguments), CommandResult(1))


def crg_config(tmp_path: Path) -> Path:
    path = write_valid_config(tmp_path / "orchestrator.toml")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n[code_review_graph]\nenabled = true\ncommand = ['crg']\n"
            "required_version = '2.3.8'\n"
        )
    (tmp_path / "repository").mkdir(exist_ok=True)
    return path


def test_doctor_identifies_missing_crg_as_non_blocking_warning(tmp_path: Path) -> None:
    checks = DoctorService(
        CrgRunner({("crg", "--version"): CommandResult(None, error="ausente")}),
        crg_config(tmp_path),
    )._check_code_review_graph()

    assert checks[0].status is CheckStatus.WARNING
    assert "pipeline fará fallback" in checks[0].message
    assert not has_errors(checks)


def test_doctor_validates_version_graph_and_both_mcp_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.code-review-graph]\ncommand="crg"\nargs=["serve"]\n',
        encoding="utf-8",
    )
    antigravity = tmp_path / ".gemini" / "antigravity"
    antigravity.mkdir(parents=True)
    (antigravity / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"code-review-graph": {
            "command": "crg", "args": ["serve"]
        }}}),
        encoding="utf-8",
    )
    repository = tmp_path / "repository"
    status_args = ("crg", "status", "--json", "--repo", str(repository))
    runner = CrgRunner({
        ("crg", "--version"): CommandResult(0, "code-review-graph 2.3.8"),
        status_args: CommandResult(0, json.dumps({"nodes": 10, "edges": 20, "files": 4})),
    })

    checks = DoctorService(runner, crg_config(tmp_path))._check_code_review_graph()

    assert all(check.status is CheckStatus.OK for check in checks)
    assert "10 nós" in checks[1].message


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


def test_cli_rejects_state_without_deep() -> None:
    result = CliRunner().invoke(app, ["doctor", "--state"])

    assert result.exit_code == 2
    assert "--state exige --deep" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_cli_deep_warns_and_displays_diagnostic_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, bool] = {}

    def diagnose(self, *, deep=False, state=False):
        received.update(deep=deep, state=state)
        return [
            DoctorCheck("Git", CheckStatus.OK, "disponível", CheckScope.LOCAL_CAPABILITY),
            DoctorCheck("Codex exec", CheckStatus.OK, "validado", CheckScope.LIVE_PROVIDER),
            DoctorCheck("Estado SQLite/GitHub", CheckStatus.WARNING, "diverge", CheckScope.STATE_CONSISTENCY),
        ]

    monkeypatch.setattr("ai_dev_orchestrator.cli.DoctorService.diagnose", diagnose)
    result = CliRunner().invoke(app, ["doctor", "--deep", "--state"])

    assert result.exit_code == 0
    assert received == {"deep": True, "state": True}
    assert "pode consumir quota/tokens" in result.output
    assert "LOCAL_CAPABILITY" in result.output
    assert "LIVE_PROVIDER" in result.output
    assert "STATE_CONSISTENCY" in result.output


def test_normal_doctor_does_not_run_deep_provider_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.sys",
        SimpleNamespace(version_info=SimpleNamespace(major=3, minor=13, micro=1)),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("provider não deve ser chamado sem --deep")

    monkeypatch.setattr("ai_dev_orchestrator.services.doctor.CodexAdapter", forbidden)
    checks = DoctorService(
        FakeRunner(successful_results()), write_valid_config(tmp_path / "orchestrator.toml")
    ).diagnose()

    assert all(check.scope is CheckScope.LOCAL_CAPABILITY for check in checks)


def test_doctor_reports_the_same_explicit_gate_used_by_pipeline(tmp_path: Path) -> None:
    config_path = write_valid_config(tmp_path / "orchestrator.toml")
    repository = tmp_path / "repository"
    (repository / "tools").mkdir(parents=True)
    (repository / "tools" / "validate.exe").touch()
    with config_path.open("a", encoding="utf-8") as config:
        config.write(
            "\n[project]\n[[project.gates]]\n"
            "name = 'official-check'\n"
            "argv = ['tools/validate.exe', '--all']\n"
        )

    checks = DoctorService(config_path=config_path)._check_project_contract()

    assert checks[0].status is CheckStatus.OK
    assert "official-check" in checks[0].message
    assert checks[1].status is CheckStatus.OK


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
