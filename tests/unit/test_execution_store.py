"""Testes locais da persistência SQLite de execuções."""

import sqlite3
from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
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
            connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
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
