"""Store SQLite local para checkpoints auditáveis de execuções."""

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from ai_dev_orchestrator.domain.execution import (
    ExecutionEvent,
    ExecutionPhase,
    RunRecord,
    TERMINAL_PHASES,
    validate_transition,
)
from ai_dev_orchestrator.domain.review import ReviewFinding, ReviewVerdict, StructuredReview

SCHEMA_VERSION = 2
_SUMMARY_LIMIT = 500


class ExecutionStoreError(Exception):
    """Falha de persistência que interrompe o control plane."""


class ActiveExecutionError(ExecutionStoreError):
    """A Issue já possui execução não terminal."""


class SchemaVersionError(ExecutionStoreError):
    """O banco tem schema incompatível."""


class SqliteExecutionStore:
    """Conexões curtas e transacionais, sem guardar conteúdo de providers."""

    def __init__(self, database_path: Path, timeout_seconds: float = 5) -> None:
        self.database_path, self.timeout_seconds = database_path, timeout_seconds
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=self.timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        return connection

    def _initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as c:
                c.execute("PRAGMA journal_mode = WAL")
                c.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
                )
                row = c.execute("SELECT version FROM schema_version").fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO schema_version(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                elif row["version"] == 1:
                    c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
                elif row["version"] != SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Versão de schema não suportada: {row['version']}"
                    )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, project_item_id TEXT, phase TEXT NOT NULL, branch TEXT, worktree_path TEXT, base_ref TEXT, codex_session_id TEXT, pull_request_number INTEGER, pull_request_url TEXT, current_head_sha TEXT, ci_head_sha TEXT, reviewed_head_sha TEXT, review_verdict TEXT, correction_attempts INTEGER NOT NULL DEFAULT 0, merge_commit_sha TEXT, merged_head_sha TEXT, project_status TEXT, last_error TEXT, terminal INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS execution_events (execution_id TEXT NOT NULL REFERENCES executions(id), sequence INTEGER NOT NULL, previous_phase TEXT, phase TEXT NOT NULL, created_at TEXT NOT NULL, summary TEXT NOT NULL, head_sha TEXT, PRIMARY KEY(execution_id, sequence))"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS review_findings (execution_id TEXT NOT NULL REFERENCES executions(id), reviewed_head_sha TEXT NOT NULL, finding_order INTEGER NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, path TEXT, line INTEGER, criterion TEXT, created_at TEXT NOT NULL, PRIMARY KEY(execution_id, reviewed_head_sha, finding_order))"
                )
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution_per_issue ON executions(issue_number) WHERE terminal = 0"
                )
        except SchemaVersionError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ExecutionStoreError(
                f"Não foi possível inicializar o banco de estado: {error}"
            ) from error

    def create(self, issue_number: int, **details: object) -> RunRecord:
        now, execution_id = _now(), str(uuid4())
        try:
            with self._connection() as c:
                c.execute(
                    "INSERT INTO executions(id, issue_number, project_item_id, phase, branch, worktree_path, base_ref, terminal, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        execution_id,
                        issue_number,
                        details.get("project_item_id"),
                        ExecutionPhase.PREPARING.value,
                        details.get("branch"),
                        details.get("worktree_path"),
                        details.get("base_ref"),
                        now,
                        now,
                    ),
                )
                c.execute(
                    "INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary) VALUES (?, 1, NULL, ?, ?, ?)",
                    (
                        execution_id,
                        ExecutionPhase.PREPARING.value,
                        now,
                        "Execução criada",
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ActiveExecutionError(
                f"A Issue #{issue_number} já possui uma execução ativa"
            ) from error
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível criar a execução: {error}"
            ) from error
        return self.get(execution_id)

    def get_active_for_issue(self, issue_number: int) -> RunRecord | None:
        return self._fetch_one(
            "SELECT * FROM executions WHERE issue_number = ? AND terminal = 0",
            (issue_number,),
        )

    def get_latest_for_issue(self, issue_number: int) -> RunRecord | None:
        return self._fetch_one(
            "SELECT * FROM executions WHERE issue_number = ? ORDER BY created_at DESC LIMIT 1",
            (issue_number,),
        )

    def get(self, execution_id: str) -> RunRecord:
        record = self._fetch_one(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        )
        if record is None:
            raise ExecutionStoreError("Execução persistida não encontrada")
        return record

    def events(self, execution_id: str) -> tuple[ExecutionEvent, ...]:
        try:
            with self._connection() as c:
                rows = c.execute(
                    "SELECT * FROM execution_events WHERE execution_id = ? ORDER BY sequence",
                    (execution_id,),
                ).fetchall()
            return tuple(
                ExecutionEvent(
                    r["execution_id"],
                    r["sequence"],
                    ExecutionPhase(r["previous_phase"])
                    if r["previous_phase"]
                    else None,
                    ExecutionPhase(r["phase"]),
                    _parse_time(r["created_at"]),
                    r["summary"],
                    r["head_sha"],
                )
                for r in rows
            )
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível consultar eventos: {error}"
            ) from error

    def transition(
        self,
        execution_id: str,
        phase: ExecutionPhase,
        *,
        summary: str,
        head_sha: str | None = None,
        **updates: object,
    ) -> RunRecord:
        current = self.get(execution_id)
        try:
            validate_transition(current.phase, phase)
        except ValueError as error:
            raise ExecutionStoreError(str(error)) from error
        if (
            "codex_session_id" in updates
            and current.codex_session_id
            and updates["codex_session_id"] != current.codex_session_id
        ):
            raise ExecutionStoreError(
                "A sessão Codex não pode ser trocada dentro da mesma execução"
            )
        allowed = {
            "project_item_id",
            "branch",
            "worktree_path",
            "base_ref",
            "codex_session_id",
            "pull_request_number",
            "pull_request_url",
            "current_head_sha",
            "ci_head_sha",
            "reviewed_head_sha",
            "review_verdict",
            "correction_attempts",
            "merge_commit_sha",
            "merged_head_sha",
            "project_status",
            "last_error",
        }
        if invalid := set(updates) - allowed:
            raise ExecutionStoreError(
                f"Campos de execução inválidos: {', '.join(sorted(invalid))}"
            )
        fields = {
            **updates,
            "phase": phase.value,
            "terminal": int(phase in TERMINAL_PHASES),
            "updated_at": _now(),
        }
        try:
            with self._connection() as c:
                sequence = c.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()[0]
                c.execute(
                    f"UPDATE executions SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
                    (*fields.values(), execution_id),
                )
                c.execute(
                    "INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution_id,
                        sequence,
                        current.phase.value,
                        phase.value,
                        fields["updated_at"],
                        _sanitize(summary),
                        head_sha,
                    ),
                )
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível registrar checkpoint: {error}"
            ) from error
        return self.get(execution_id)

    def fail(self, execution_id: str, error: Exception | str) -> RunRecord:
        return self.transition(
            execution_id,
            ExecutionPhase.FAILED,
            summary="Execução interrompida",
            last_error=_sanitize(str(error)),
        )

    def checkpoint(
        self,
        execution_id: str,
        *,
        summary: str,
        head_sha: str | None = None,
        **updates: object,
    ) -> RunRecord:
        """Registra informação nova sem avançar a fase do control plane."""
        current = self.get(execution_id)
        allowed = {
            "project_item_id",
            "branch",
            "worktree_path",
            "base_ref",
            "codex_session_id",
            "pull_request_number",
            "pull_request_url",
            "current_head_sha",
            "ci_head_sha",
            "reviewed_head_sha",
            "review_verdict",
            "correction_attempts",
            "merge_commit_sha",
            "merged_head_sha",
            "project_status",
            "last_error",
        }
        if invalid := set(updates) - allowed:
            raise ExecutionStoreError(
                f"Campos de execução inválidos: {', '.join(sorted(invalid))}"
            )
        if (
            "codex_session_id" in updates
            and current.codex_session_id
            and updates["codex_session_id"] != current.codex_session_id
        ):
            raise ExecutionStoreError(
                "A sessão Codex não pode ser trocada dentro da mesma execução"
            )
        fields = {**updates, "updated_at": _now()}
        try:
            with self._connection() as c:
                sequence = c.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()[0]
                c.execute(
                    f"UPDATE executions SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
                    (*fields.values(), execution_id),
                )
                c.execute(
                    "INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution_id,
                        sequence,
                        current.phase.value,
                        current.phase.value,
                        fields["updated_at"],
                        _sanitize(summary),
                        head_sha,
                    ),
                )
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível registrar checkpoint: {error}"
            ) from error
        return self.get(execution_id)

    def record_review(
        self, execution_id: str, review: StructuredReview, summary: str
    ) -> RunRecord:
        """Persiste veredito, findings e evento em uma única transação."""
        try:
            with self._connection() as c:
                row = c.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
                if row is None:
                    raise ExecutionStoreError("Execução persistida não encontrada")
                current = _record(row)
                if current.phase != ExecutionPhase.GEMINI_REVIEWING:
                    raise ExecutionStoreError("Review só pode ser registrada em GEMINI_REVIEWING")
                if not current.current_head_sha or review.reviewed_head_sha != current.current_head_sha:
                    raise ExecutionStoreError("Review não corresponde ao HEAD atual")
                if review.verdict == ReviewVerdict.REJECTED and not review.findings:
                    raise ExecutionStoreError("Review rejeitada exige ao menos um finding")
                now = _now()
                c.execute("DELETE FROM review_findings WHERE execution_id = ? AND reviewed_head_sha = ?", (execution_id, review.reviewed_head_sha))
                for order, finding in enumerate(review.findings, start=1):
                    c.execute(
                        "INSERT INTO review_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (execution_id, review.reviewed_head_sha, order, finding.severity.value,
                         _sanitize_finding(finding.title, 300), _sanitize_finding(finding.description, 4000),
                         _sanitize_finding(finding.path, 500) if finding.path else None,
                         finding.line, _sanitize_finding(finding.criterion, 500) if finding.criterion else None, now),
                    )
                sequence = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_events WHERE execution_id = ?", (execution_id,)).fetchone()[0]
                c.execute("UPDATE executions SET reviewed_head_sha = ?, review_verdict = ?, updated_at = ? WHERE id = ?", (review.reviewed_head_sha, review.verdict.value, now, execution_id))
                c.execute("INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)", (execution_id, sequence, current.phase.value, current.phase.value, now, _sanitize(summary), review.reviewed_head_sha))
        except ExecutionStoreError:
            raise
        except sqlite3.Error as error:
            raise ExecutionStoreError(f"Não foi possível registrar review: {error}") from error
        return self.get(execution_id)

    def review_findings(self, execution_id: str, reviewed_head_sha: str | None = None) -> tuple[ReviewFinding, ...]:
        sql = "SELECT * FROM review_findings WHERE execution_id = ?"
        parameters: tuple[object, ...] = (execution_id,)
        if reviewed_head_sha is not None:
            sql += " AND reviewed_head_sha = ?"
            parameters += (reviewed_head_sha,)
        sql += " ORDER BY created_at, reviewed_head_sha, finding_order"
        try:
            with self._connection() as c:
                rows = c.execute(sql, parameters).fetchall()
            from ai_dev_orchestrator.domain.review import FindingSeverity
            return tuple(ReviewFinding(FindingSeverity(row["severity"]), row["title"], row["description"], row["path"], row["line"], row["criterion"]) for row in rows)
        except sqlite3.Error as error:
            raise ExecutionStoreError(f"Não foi possível consultar findings: {error}") from error

    def _fetch_one(self, sql: str, parameters: tuple[object, ...]) -> RunRecord | None:
        try:
            with self._connection() as c:
                row = c.execute(sql, parameters).fetchone()
            return _record(row) if row else None
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível consultar execução: {error}"
            ) from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sanitize(value: str) -> str:
    return re.sub(
        r"(?i)(token|authorization|password|secret)\s*[:=]\s*\S+",
        r"\1=[redigido]",
        value.replace("\n", " "),
    )[:_SUMMARY_LIMIT]


def _sanitize_finding(value: str, limit: int) -> str:
    return _sanitize(value)[:limit]


def _record(row: sqlite3.Row) -> RunRecord:
    excluded = {"id", "issue_number", "phase", "created_at", "updated_at", "terminal"}
    return RunRecord(
        row["id"],
        row["issue_number"],
        ExecutionPhase(row["phase"]),
        _parse_time(row["created_at"]),
        _parse_time(row["updated_at"]),
        **{key: row[key] for key in row.keys() if key not in excluded},
    )
