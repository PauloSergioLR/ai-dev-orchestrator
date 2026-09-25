"""Testes da identificação local do Codex sem depender de uma instalação real."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ai_dev_orchestrator.adapters.codex import CodexAdapter
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.infrastructure.codex_runtime import codex_candidates, global_codex_settings
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.doctor import CheckStatus, DoctorService
from ai_dev_orchestrator.services.inspect import InspectService
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


class FakeRunner:
    def __init__(self, output: str = "codex 1.2.3\n", secondary_version: str = "codex 2.0\n") -> None:
        self.output = output
        self.secondary_version = secondary_version
        self.arguments: list[list[str]] = []

    def run(self, arguments, **kwargs):
        self.arguments.append(list(arguments))
        if arguments[-1:] == ["--version"]:
            if "broken" in arguments[0]:
                return CommandResult(1, error="inacessível")
            if "npm" in arguments[0]:
                return CommandResult(0, self.secondary_version)
            return CommandResult(0, self.output)
        return CommandResult(0)


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture", encoding="utf-8")
    return str(path)


def test_one_candidate_and_known_windows_shims(tmp_path: Path) -> None:
    executable = _touch(tmp_path / "official" / "codex")
    shim_cmd = _touch(tmp_path / "Volta" / "bin" / "codex.cmd")
    shim_exe = _touch(tmp_path / "npm" / "codex.exe")
    assert codex_candidates(executable) == ()  # PATH espera diretórios.
    candidates = codex_candidates(str(Path(executable).parent))
    assert [candidate.path for candidate in candidates] == [executable]
    if os.name == "nt":
        assert codex_candidates(str(Path(shim_cmd).parent))[0].path == shim_cmd
        assert codex_candidates(str(Path(shim_exe).parent))[0].path == shim_exe


def test_doctor_lists_multiple_candidates_and_version_divergence(monkeypatch, tmp_path: Path) -> None:
    first = _touch(tmp_path / "Volta" / "bin" / "codex.exe")
    second = _touch(tmp_path / "npm" / "codex.cmd")
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.codex_candidates",
        lambda: (
            type("Candidate", (), {"path": first, "origin": "shim Volta"})(),
            type("Candidate", (), {"path": second, "origin": "instalação/shim npm"})(),
        ),
    )
    runner = FakeRunner("codex 1.0\n")
    check = DoctorService(runner)._check_codex_identity()
    assert check.status is CheckStatus.OK
    assert "aviso: 2 candidatos" in check.message
    assert "versões divergentes" in check.message
    assert check.message.startswith(f"usado: {first} ")
    assert "npm\\codex.cmd" in check.message


def test_doctor_reports_multiple_candidates_with_same_version(monkeypatch, tmp_path: Path) -> None:
    first = _touch(tmp_path / "official" / "codex.exe")
    second = _touch(tmp_path / "npm" / "codex.exe")
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.codex_candidates",
        lambda: tuple(type("Candidate", (), {"path": item, "origin": "PATH"})() for item in (first, second)),
    )
    check = DoctorService(FakeRunner(secondary_version="codex 1.2.3\n"))._check_codex_identity()
    assert "aviso: 2 candidatos" in check.message
    assert "versões divergentes" not in check.message


def test_broken_secondary_does_not_break_doctor(monkeypatch, tmp_path: Path) -> None:
    first = _touch(tmp_path / "codex.exe")
    second = _touch(tmp_path / "broken" / "codex.exe")
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.doctor.codex_candidates",
        lambda: tuple(type("Candidate", (), {"path": item, "origin": "PATH"})() for item in (first, second)),
    )
    check = DoctorService(FakeRunner())._check_codex_identity()
    assert check.status is CheckStatus.OK
    assert "indisponível" in check.message


def test_global_settings_reads_only_allowed_keys_and_handles_malformed_config(
    monkeypatch, tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config.write_text(
        'model = "gpt-test"\nmodel_reasoning_effort = "high"\n'
        'api_key = "never-show-this"\n',
        encoding="utf-8",
    )
    model, effort, _ = global_codex_settings()
    assert (model, effort) == ("gpt-test", "high")
    config.write_text('model = "unterminated\n', encoding="utf-8")
    assert global_codex_settings() == (None, None, None)
    config.unlink()
    assert global_codex_settings() == (None, None, None)


def test_adapter_records_selection_source_without_real_codex(monkeypatch, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    class CodexFakeRunner:
        def run(self, arguments, input_text=None, **kwargs):
            output = '\n'.join((
                '{"type":"thread.started","thread_id":"synthetic-session"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
                '{"type":"turn.completed"}',
            ))
            return CommandResult(0, output)

    monkeypatch.setattr("ai_dev_orchestrator.adapters.codex.effective_executable", lambda: "C:/codex.exe")
    monkeypatch.setattr("ai_dev_orchestrator.adapters.codex.global_codex_settings", lambda: ("gpt-global", "high", None))
    execution = CodexAdapter(CodexFakeRunner()).execute(worktree, "prompt")
    assert execution.executable_path == "C:/codex.exe"
    assert execution.cli_version is None  # Runner sintético não inicia processo real.
    assert execution.model_source == "codex-default"
    assert execution.reasoning_effort == "high"


def test_explicit_model_source_is_orchestrator(tmp_path: Path) -> None:
    config = tmp_path / "orchestrator.toml"
    repository = tmp_path / "repo"
    repository.mkdir()
    config.write_text(
        "[github]\nowner='a'\nrepository='b'\nproject_number=1\nready_status='Ready'\n"
        f"[workspace]\nrepository_path='{repository.as_posix()}'\n"
        f"worktrees_dir='{(tmp_path / 'worktrees').as_posix()}'\nbase_ref='main'\n"
        "[execution]\nmax_attempts=1\nmax_parallel_runs=1\nauto_merge=false\n"
        f"[state]\ndatabase_path='{(tmp_path / 'state.db').as_posix()}'\n"
        "[providers]\ncodex_model='gpt-explicit'\n",
        encoding="utf-8",
    )
    check = DoctorService(config_path=config)._check_codex_model_source()
    assert check.message == "gpt-explicit; origem: orchestrator.toml"


def test_default_model_source_is_delegated_without_exposing_secrets(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config.write_text(
        'model = "gpt-global"\nmodel_reasoning_effort = "medium"\n'
        'api_key = "secret-value"\n',
        encoding="utf-8",
    )
    orchestrator = tmp_path / "orchestrator.toml"
    repository = tmp_path / "repo"
    repository.mkdir()
    orchestrator.write_text(
        "[github]\nowner='a'\nrepository='b'\nproject_number=1\nready_status='Ready'\n"
        f"[workspace]\nrepository_path='{repository.as_posix()}'\n"
        f"worktrees_dir='{(tmp_path / 'worktrees').as_posix()}'\nbase_ref='main'\n"
        "[execution]\nmax_attempts=1\nmax_parallel_runs=1\nauto_merge=false\n"
        f"[state]\ndatabase_path='{(tmp_path / 'state.db').as_posix()}'\n",
        encoding="utf-8",
    )
    message = DoctorService(config_path=orchestrator)._check_codex_model_source().message
    assert "delegado ao Codex CLI/configuração global" in message
    assert "model=gpt-global" in message and "model_reasoning_effort=medium" in message
    assert "secret-value" not in message


def test_runtime_metadata_roundtrip_and_legacy_inspect(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    current = store.create(103, codex_model="default")
    persisted = store.checkpoint(
        current.id,
        summary="metadados Codex",
        codex_executable_path="C:/Program Files/Codex/codex.exe",
        codex_cli_version="codex 1.2.3",
        codex_model_source="codex-default",
        codex_reasoning_effort="high",
    )
    assert persisted.codex_cli_version == "codex 1.2.3"
    diagnosis = InspectService(store).inspect(103)
    assert diagnosis.codex_runtime["model_source"] == "codex-default"
    assert "never-show-this" not in json.dumps(diagnosis.as_dict())
    legacy = store.create(104)
    legacy_view = InspectService(store).inspect(104)
    assert legacy_view.codex_runtime == {
        "executable_path": None, "cli_version": None,
        "model_source": None, "reasoning_effort": None,
    }
    assert legacy.phase is ExecutionPhase.PREPARING


def test_resume_logs_path_or_version_change_without_replacing_identity(monkeypatch, tmp_path: Path) -> None:
    from ai_dev_orchestrator.services.resume import ResumeService

    store = SqliteExecutionStore(tmp_path / "state.db")
    original = store.create(105)
    original = store.checkpoint(
        original.id, summary="started", codex_executable_path="C:/old/codex.exe",
        codex_cli_version="codex 1.0",
    )
    monkeypatch.setattr("ai_dev_orchestrator.services.resume.effective_executable", lambda: "C:/new/codex.exe")
    monkeypatch.setattr(
        "ai_dev_orchestrator.services.resume.CommandRunner",
        lambda **kwargs: FakeRunner("codex 2.0\n"),
    )
    service = ResumeService.__new__(ResumeService)
    service.store = store
    updated = service._record_codex_identity_change(original)
    assert updated.id == original.id
    assert updated.codex_session_id == original.codex_session_id
    assert updated.codex_executable_path == "C:/old/codex.exe"
    event = store.events(original.id)[-1]
    assert "caminho C:/old/codex.exe" in event.summary
    assert "versão codex 1.0 → codex 2.0" in event.summary
