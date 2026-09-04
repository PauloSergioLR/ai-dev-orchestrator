"""Testes locais da persistência SQLite de execuções."""

import sqlite3
from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.review import FindingSeverity, ReviewFinding, ReviewVerdict, StructuredReview
from ai_dev_orchestrator.infrastructure.database import (
    ActiveExecutionError,
    SchemaVersionError,
    SqliteExecutionStore,
)


def test_creates_versioned_schema_and_reopens_without_losing_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "orchestrator.db"
    first = SqliteExecutionStore(path)
    created = first.create(35, branch="feat/state")

    reopened = SqliteExecutionStore(path)

    assert reopened.get(created.id).branch == "feat/state"
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        )


def test_refuses_two_active_executions_and_keeps_ordered_journal(
    tmp_path: Path,
) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(35)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")

    with pytest.raises(ActiveExecutionError):
        store.create(35)

    assert [event.sequence for event in store.events(run.id)] == [1, 2, 3]
    with pytest.raises(Exception, match="inválida"):
        store.transition(run.id, ExecutionPhase.MERGING, summary="salto")


def test_preserves_session_and_sanitizes_limited_error(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(35)
    store.transition(
        run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="same"
    )

    with pytest.raises(Exception, match="sessão Codex"):
        store.checkpoint(run.id, summary="troca", codex_session_id="other")

    failed = store.fail(run.id, "token=super-secreto\n" + "x" * 1000)
    assert "super-secreto" not in failed.last_error
    assert len(failed.last_error or "") <= 500


def test_unknown_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (99)")

    with pytest.raises(SchemaVersionError, match="não suportada"):
        SqliteExecutionStore(path)


def test_records_structured_review_atomically_and_redacts_findings(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    code = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    testing = store.transition(code.id, ExecutionPhase.TESTING, summary="Gates", codex_session_id="same")
    commit = store.transition(testing.id, ExecutionPhase.COMMIT_PENDING, summary="Commit", current_head_sha="a" * 40)
    push = store.transition(commit.id, ExecutionPhase.PUSH_PENDING, summary="Push")
    pr = store.transition(push.id, ExecutionPhase.PR_PENDING, summary="PR")
    ci = store.transition(pr.id, ExecutionPhase.WAITING_CI, summary="CI")
    review_run = store.transition(ci.id, ExecutionPhase.GEMINI_REVIEWING, summary="Review")
    review = StructuredReview(ReviewVerdict.REJECTED, (ReviewFinding(FindingSeverity.HIGH, "token=abc", "password=abc"),), "a" * 40, "x")
    recorded = store.record_review(review_run.id, review, "Review persistida")
    findings = store.review_findings(recorded.id, "a" * 40)
    assert recorded.review_verdict == "REJECTED"
    assert findings[0].title.endswith("[redigido]")
    assert "abc" not in findings[0].description


def test_finding_description_uses_its_own_limit(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    code = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="x")
    testing = store.transition(code.id, ExecutionPhase.TESTING, summary="x", codex_session_id="same")
    commit = store.transition(testing.id, ExecutionPhase.COMMIT_PENDING, summary="x", current_head_sha="a" * 40)
    push = store.transition(commit.id, ExecutionPhase.PUSH_PENDING, summary="x")
    pr = store.transition(push.id, ExecutionPhase.PR_PENDING, summary="x")
    ci = store.transition(pr.id, ExecutionPhase.WAITING_CI, summary="x")
    reviewing = store.transition(ci.id, ExecutionPhase.GEMINI_REVIEWING, summary="x")
    description = "d" * 900
    review = StructuredReview(ReviewVerdict.REJECTED, (ReviewFinding(FindingSeverity.LOW, "título", description),), "a" * 40, "x")
    store.record_review(reviewing.id, review, "x")
    assert len(store.review_findings(reviewing.id)[0].description) == 900


def test_migrates_schema_v1_preserving_execution_and_journal(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    execution_id = "legacy-run"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1)")
        connection.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, project_item_id TEXT, phase TEXT NOT NULL, branch TEXT, worktree_path TEXT, base_ref TEXT, codex_session_id TEXT, pull_request_number INTEGER, pull_request_url TEXT, current_head_sha TEXT, ci_head_sha TEXT, reviewed_head_sha TEXT, review_verdict TEXT, correction_attempts INTEGER NOT NULL DEFAULT 0, merge_commit_sha TEXT, merged_head_sha TEXT, project_status TEXT, last_error TEXT, terminal INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE execution_events (execution_id TEXT NOT NULL REFERENCES executions(id), sequence INTEGER NOT NULL, previous_phase TEXT, phase TEXT NOT NULL, created_at TEXT NOT NULL, summary TEXT NOT NULL, head_sha TEXT, PRIMARY KEY(execution_id, sequence))")
        connection.execute("INSERT INTO executions VALUES (?, 37, 'item', 'TESTING', 'feat/x', 'C:/worktree', 'main', 'session', NULL, NULL, ?, NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL, 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')", (execution_id, "a" * 40))
        connection.execute("INSERT INTO execution_events VALUES (?, 1, NULL, 'PREPARING', '2026-01-01T00:00:00+00:00', 'criada', NULL)", (execution_id,))
        connection.execute("INSERT INTO execution_events VALUES (?, 2, 'PREPARING', 'TESTING', '2026-01-01T00:01:00+00:00', 'gates', ?)", (execution_id, "a" * 40))
    store = SqliteExecutionStore(path)
    assert store.get_active_for_issue(37).id == execution_id
    assert [event.sequence for event in store.events(execution_id)] == [1, 2]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'review_findings'").fetchone()
