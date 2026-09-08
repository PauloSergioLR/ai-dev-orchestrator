"""Resolução e preflight reais do CommandRunner, com fronteira de processo simulada."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter, AntigravityError
from ai_dev_orchestrator.config import load_config
from ai_dev_orchestrator.services.doctor import CheckStatus, DoctorService


def configuration(tmp_path, executable):
    path = tmp_path / "orchestrator.toml"
    path.write_text(
        "[github]\nowner='a'\nrepository='b'\nproject_number=1\nready_status='Ready'\n"
        f"[workspace]\nrepository_path='{tmp_path.as_posix()}'\n"
        f"worktrees_dir='{tmp_path.as_posix()}'\nbase_branch='main'\n"
        "[execution]\nmax_attempts=1\nmax_parallel_runs=1\nauto_merge=false\n"
        f"[review]\nexecutable='{executable}'\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("absolute", [True, False])
def test_doctor_and_runtime_resolve_same_configured_executable(tmp_path, monkeypatch, available, absolute):
    executable = (tmp_path / "CLI com espaços" / "agy.exe").as_posix() if absolute else "agy"
    path = configuration(tmp_path, executable)
    resolved, calls = [], []

    def which(command):
        resolved.append(command)
        return command if available else None

    def run(args, **options):
        calls.append((args, options))
        if args[-1] == "--version":
            output = "1.1.27"
        elif args[-1] == "--help":
            output = (Path(__file__).parents[1] / "fixtures/antigravity/help-1.1.27.txt").read_text(encoding="utf-8")
        else:
            output = '{"status":"SUCCESS","structured_output":{"message":"ok"}}'
        return subprocess.CompletedProcess(args, 0, output.encode("utf-8"), b"")

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)
    config = load_config(path)
    adapter = AntigravityAdapter(12, executable=config.review.executable)
    check = DoctorService(config_path=path)._check_antigravity_cli()
    if not available:
        assert check.status is CheckStatus.ERROR
        with pytest.raises(AntigravityError, match="ORCH_REVIEW__EXECUTABLE"):
            adapter.invoke("p", tmp_path, {})
        assert calls == []
    else:
        assert check.status is CheckStatus.OK
        prompt = "á漢😀\n" + "x" * 100_000
        assert json.loads(adapter.invoke(prompt, tmp_path, {})) == {"message": "ok"}
        assert [call[0] for call in calls[:2]] == [call[0] for call in calls[2:4]]
        args, options = calls[-1]
        assert args[0] == executable and prompt not in args
        assert options["input"] == prompt.encode("utf-8")
        assert options["cwd"] == tmp_path
        assert options["timeout"] == 12
        assert options["shell"] is False
    assert resolved and set(resolved) == {executable}


def test_environment_overrides_reviewer_executable(tmp_path, monkeypatch):
    path = configuration(tmp_path, "agy")
    monkeypatch.setenv("ORCH_REVIEW__EXECUTABLE", "/tools/agy")
    assert load_config(path).review.executable == "/tools/agy"
