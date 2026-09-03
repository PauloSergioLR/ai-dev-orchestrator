"""Testes da retomada sem rede nem um segundo run persistido."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery import RecoveryError, ResumeService


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
        state={"database_path": tmp_path / "state.db"},
    )


class _Validator:
    calls = 0

    def validate(self, worktree: Path):
        self.calls += 1
        return ()


class _Pipeline:
    def __init__(self, validator: _Validator) -> None:
        self._execution_id = None
        self.worktree_creator = object()
        self.git_publisher = None
        self.review_reader = None
        self.local_validator = validator


def test_resume_testing_reuses_the_same_execution_and_reexecutes_gates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37, branch="feat/recovery", worktree_path=str(worktree), base_ref="main")
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="sessao")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")
    validator = _Validator()

    recovered = ResumeService(config, _Pipeline(validator), store).resume(37)  # type: ignore[arg-type]

    assert recovered.id == run.id
    assert recovered.phase is ExecutionPhase.PUBLISHING
    assert recovered.codex_session_id == "sessao"
    assert validator.calls == 1


def test_resume_refuses_terminal_execution(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    store.fail(run.id, "falha")

    with pytest.raises(RecoveryError, match="terminal"):
        ResumeService(config, _Pipeline(_Validator()), store).resume(37)  # type: ignore[arg-type]
