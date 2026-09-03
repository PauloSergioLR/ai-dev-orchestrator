"""Testes da retomada sem rede nem um segundo run persistido."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.worktree import GitWorktree
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


class _Publisher:
    def __init__(self, local: str, remote: str) -> None:
        self.local, self.remote = local, remote
        self.pushes = 0

    def merge_state(self, worktree: Path) -> tuple[str, str]:
        return "feat/execution-recovery", self.local

    def remote_head(self, worktree: Path, remote: str, branch: str) -> str:
        return self.remote

    def is_ancestor(self, worktree: Path, ancestor: str, descendant: str) -> bool:
        return ancestor == "4ac557b7f9e39fbb31883e336bb5b24ef2535073" and descendant == self.local

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.pushes += 1


class _ReviewReader:
    def __init__(self, head: str) -> None:
        self.head = head
        self.calls = 0

    def get_review_data(self, number: int) -> dict[str, object]:
        self.calls += 1
        return {"number": 38, "url": "https://github.com/acme/repo/pull/38", "state": "OPEN",
                "baseRefName": "main", "headRefName": "feat/execution-recovery", "headRefOid": self.head}


def test_reconciles_real_crash_window_with_new_head_already_on_remote_and_pr(tmp_path: Path) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    config = _config(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37, branch="feat/execution-recovery", worktree_path=str(worktree), base_ref="main")
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="sessao")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")
    run = store.transition(run.id, ExecutionPhase.PUBLISHING, summary="Publicar", current_head_sha=old,
                           pull_request_number=38, pull_request_url="https://github.com/acme/repo/pull/38",
                           correction_attempts=1)
    pipeline = _Pipeline(_Validator())
    pipeline.git_publisher = _Publisher(new, new)
    pipeline.review_reader = _ReviewReader(new)
    pipeline._execution_id = run.id
    service = ResumeService(config, pipeline, store)  # type: ignore[arg-type]

    recovered = service._reconcile_publication(
        run, GitWorktree(tmp_path / "repo", worktree, "feat/execution-recovery", "main"),
        service._validate_pull_request(run),
    )

    assert recovered.id == run.id
    assert recovered.current_head_sha == new
    assert recovered.phase is ExecutionPhase.WAITING_CI
    assert pipeline.git_publisher.pushes == 0
    assert recovered.codex_session_id == "sessao"


def test_pr_propagation_accepts_only_previous_head_until_expected_head_arrives(tmp_path: Path, monkeypatch) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    reader = _ReviewReader(old)
    pipeline = _Pipeline(_Validator())
    pipeline.review_reader = reader
    service = ResumeService(_config(tmp_path), pipeline, SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]
    monkeypatch.setattr("ai_dev_orchestrator.services.recovery.time.sleep", lambda _: setattr(reader, "head", new))

    pull_request = service._wait_for_pull_request_head(
        PullRequest(38, "https://github.com/acme/repo/pull/38", "", "main", "feat/execution-recovery", old), new, old
    )

    assert pull_request.head_sha == new
    assert reader.calls == 2
