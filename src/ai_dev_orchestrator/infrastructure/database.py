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

SCHEMA_VERSION = 3
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
                elif row["version"] in {1, 2}:
                    c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
                elif row["version"] != SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Versão de schema não suportada: {row['version']}"
                    )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, project_item_id TEXT, phase TEXT NOT NULL, branch TEXT, worktree_path TEXT, base_ref TEXT, codex_session_id TEXT, pull_request_number INTEGER, pull_request_url TEXT, current_head_sha TEXT, ci_head_sha TEXT, reviewed_head_sha TEXT, review_verdict TEXT, correction_attempts INTEGER NOT NULL DEFAULT 0, merge_commit_sha TEXT, merged_head_sha TEXT, project_status TEXT, last_error TEXT, terminal INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                existing_columns = {
                    column["name"]
                    for column in c.execute("PRAGMA table_info(executions)").fetchall()
                }
                additions = {
                    "codex_model": "TEXT NOT NULL DEFAULT 'default'",
                    "gemini_model": "TEXT NOT NULL DEFAULT 'default'",
                    "quota_provider": "TEXT",
                    "quota_classification": "TEXT",
                    "quota_observed_at": "TEXT",
                    "quota_retry_at": "TEXT",
                    "provider_resume_phase": "TEXT",
                    "codex_start_attempted": "INTEGER NOT NULL DEFAULT 0",
                    "provider_retry_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "cleanup_status": "TEXT NOT NULL DEFAULT 'PENDING'",
                    "cleanup_detail": "TEXT",
                    "codex_tokens": "INTEGER",
                    "gemini_tokens": "INTEGER",
                    "codex_cost": "REAL",
                    "gemini_cost": "REAL",
                }
                for name, declaration in additions.items():
                    if name not in existing_columns:
                        c.execute(f"ALTER TABLE executions ADD COLUMN {name} {declaration}")
                        if name == "codex_start_attempted":
                            # O schema antigo não prova que CODEX_RUNNING ainda não chamou a CLI.
                            c.execute("UPDATE executions SET codex_start_attempted = 1 WHERE phase != 'PREPARING' OR codex_session_id IS NOT NULL")
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
        if any(run.issue_number == issue_number for run in self.list_historical_candidates()):
            raise ActiveExecutionError("Execução histórica transitória exige --recover-failed; novo run recusado")
        now, execution_id = _now(), str(uuid4())
        try:
            with self._connection() as c:
                c.execute(
                    "INSERT INTO executions(id, issue_number, project_item_id, phase, branch, worktree_path, base_ref, codex_model, gemini_model, terminal, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        execution_id,
                        issue_number,
                        details.get("project_item_id"),
                        ExecutionPhase.PREPARING.value,
                        details.get("branch"),
                        details.get("worktree_path"),
                        details.get("base_ref"),
                        details.get("codex_model", "default"),
                        details.get("gemini_model", "default"),
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

    def list_historical_candidates(self) -> tuple[RunRecord, ...]:
        from ai_dev_orchestrator.domain.historical import is_historical_candidate
        return tuple(run for run in self.list_history() if is_historical_candidate(run))

    def list_reconciliation_required(self) -> tuple[RunRecord, ...]:
        """FAILED publicado nunca é ignorado pelo scheduler antes de uma decisão humana."""
        return tuple(
            run for run in self.list_history()
            if run.phase is ExecutionPhase.FAILED and run.pull_request_number is not None
        )

    def supersede(self, execution_id: str, *, summary: str) -> RunRecord:
        """Encerra auditavelmente um run deliberadamente substituído pelo usuário.

        Esta é a única transição para SUPERSEDED. Ela não altera nenhum checkpoint
        de identidade nem faz I/O fora do SQLite.
        """
        current = self.get(execution_id)
        if current.phase in {ExecutionPhase.COMPLETED, ExecutionPhase.SUPERSEDED}:
            raise ExecutionStoreError("Execução concluída ou já supersedida não pode ser abandonada")
        try:
            with self._connection() as c:
                sequence = c.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()[0]
                now = _now()
                c.execute(
                    "UPDATE executions SET phase = ?, terminal = 1, updated_at = ? WHERE id = ?",
                    (ExecutionPhase.SUPERSEDED.value, now, execution_id),
                )
                c.execute(
                    "INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (execution_id, sequence, current.phase.value, ExecutionPhase.SUPERSEDED.value,
                     now, _sanitize(summary), current.current_head_sha),
                )
        except sqlite3.Error as error:
            raise ExecutionStoreError(f"Não foi possível superseder execução: {error}") from error
        return self.get(execution_id)

    def require_human(self, execution_id: str, *, summary: str) -> RunRecord:
        """Promove uma divergência remota a checkpoint explícito, sem descartá-la."""
        current = self.get(execution_id)
        if current.phase in TERMINAL_PHASES:
            raise ExecutionStoreError("Execução terminal não pode exigir nova intervenção")
        if current.phase is ExecutionPhase.HUMAN_REQUIRED:
            return current
        try:
            with self._connection() as c:
                sequence = c.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()[0]
                now = _now()
                c.execute("UPDATE executions SET phase = ?, updated_at = ? WHERE id = ?",
                          (ExecutionPhase.HUMAN_REQUIRED.value, now, execution_id))
                c.execute(
                    "INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (execution_id, sequence, current.phase.value, ExecutionPhase.HUMAN_REQUIRED.value,
                     now, _sanitize(summary), current.current_head_sha),
                )
        except sqlite3.Error as error:
            raise ExecutionStoreError(f"Não foi possível registrar intervenção humana: {error}") from error
        return self.get(execution_id)

    def reactivate_historical(self, expected: RunRecord, observation, planner) -> RunRecord:
        """Única exceção à terminalidade; transação compara identidade e audita a prova."""
        from ai_dev_orchestrator.domain.historical import historical_target, validate_historical
        try:
            with self._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM executions WHERE id = ?", (expected.id,)).fetchone()
                if row is None or _record(row) != expected:
                    raise ExecutionStoreError("Execução mudou desde a prova histórica")
                if c.execute("SELECT 1 FROM executions WHERE terminal = 0 LIMIT 1").fetchone():
                    raise ActiveExecutionError("Outra execução ativa impede reativação")
                target = historical_target(expected, self.events(expected.id))
                validate_historical(expected, target, observation, planner)
                now = _now()
                c.execute("UPDATE executions SET phase = ?, terminal = 0, updated_at = ?, quota_retry_at = NULL, provider_resume_phase = NULL, provider_retry_attempts = 0 WHERE id = ?",
                          (target.value, now, expected.id))
                sequence = c.execute("SELECT MAX(sequence) + 1 FROM execution_events WHERE execution_id = ?", (expected.id,)).fetchone()[0]
                c.execute("INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (expected.id, sequence, expected.phase.value, target.value, now,
                           "FAILED transitório reconciliado: worktree, sessão, Issue, PR, HEADs, merge e Project comprovados", expected.current_head_sha))
        except (sqlite3.Error, ValueError) as error:
            raise ExecutionStoreError(f"Reativação histórica recusada: {error}") from error
        return self.get(expected.id)

    def list_active(self) -> tuple[RunRecord, ...]:
        """Lista execuções não terminais para coordenação sequencial."""
        try:
            with self._connection() as c:
                rows = c.execute(
                    "SELECT * FROM executions WHERE terminal = 0 "
                    "ORDER BY created_at, id"
                ).fetchall()
            return tuple(_record(row) for row in rows)
        except sqlite3.Error as error:
            raise ExecutionStoreError(
                f"Não foi possível consultar execuções ativas: {error}"
            ) from error

    def get_latest_for_issue(self, issue_number: int) -> RunRecord | None:
        return self._fetch_one(
            "SELECT * FROM executions WHERE issue_number = ? ORDER BY created_at DESC LIMIT 1",
            (issue_number,),
        )

    def list_history(self, issue_number: int | None = None) -> tuple[RunRecord, ...]:
        """Lista o journal de execuções em ordem estável, sem reescrever evidências."""
        sql = "SELECT * FROM executions"
        parameters: tuple[object, ...] = ()
        if issue_number is not None:
            sql += " WHERE issue_number = ?"
            parameters = (issue_number,)
        sql += " ORDER BY created_at DESC, id DESC"
        try:
            with self._connection() as c:
                rows = c.execute(sql, parameters).fetchall()
            return tuple(_record(row) for row in rows)
        except sqlite3.Error as error:
            raise ExecutionStoreError("Não foi possível consultar o histórico: " + str(error)) from error

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
            "codex_model",
            "gemini_model",
            "quota_provider",
            "quota_classification",
            "quota_observed_at",
            "quota_retry_at",
            "provider_resume_phase",
            "codex_start_attempted",
            "provider_retry_attempts",
            "cleanup_status",
            "cleanup_detail",
            "codex_tokens",
            "gemini_tokens",
            "codex_cost",
            "gemini_cost",
        }
        if invalid := set(updates) - allowed:
            raise ExecutionStoreError(
                f"Campos de execução inválidos: {', '.join(sorted(invalid))}"
            )
        self._validate_models(current, updates)
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
            "codex_model",
            "gemini_model",
            "quota_provider",
            "quota_classification",
            "quota_observed_at",
            "quota_retry_at",
            "provider_resume_phase",
            "codex_start_attempted",
            "provider_retry_attempts",
            "cleanup_status",
            "cleanup_detail",
            "codex_tokens",
            "gemini_tokens",
            "codex_cost",
            "gemini_cost",
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
        self._validate_models(current, updates)
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

    @staticmethod
    def _validate_models(current: RunRecord, updates: dict[str, object]) -> None:
        if current.codex_start_attempted and updates.get("codex_start_attempted", True) is not True:
            raise ExecutionStoreError("O início da primeira chamada Codex não pode ser apagado")
        for field in ("codex_model", "gemini_model"):
            if field in updates and getattr(current, field) != updates[field]:
                raise ExecutionStoreError(
                    "O modelo do provider não pode ser trocado dentro da mesma execução"
                )

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
                c.execute("UPDATE executions SET reviewed_head_sha = ?, review_verdict = ?, updated_at = ?, provider_retry_attempts = 0 WHERE id = ?", (review.reviewed_head_sha, review.verdict.value, now, execution_id))
                c.execute("INSERT INTO execution_events(execution_id, sequence, previous_phase, phase, created_at, summary, head_sha) VALUES (?, ?, ?, ?, ?, ?, ?)", (execution_id, sequence, current.phase.value, current.phase.value, now, _sanitize(summary), review.reviewed_head_sha))
        except ExecutionStoreError:
            raise
        except sqlite3.Error as error:
            raise ExecutionStoreError(f"Não foi possível registrar review: {error}") from error
        return self.get(execution_id)

    def record_usage(
        self, execution_id: str, provider: str, *, tokens: int | None = None,
        cost: float | None = None,
    ) -> RunRecord:
        """Guarda somente números estruturados fornecidos pelo provider, nunca logs."""
        if provider not in {"codex", "gemini"}:
            raise ExecutionStoreError("Provider de uso não suportado")
        if tokens is None and cost is None:
            return self.get(execution_id)
        if (tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0)) or (
            cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0)
        ):
            raise ExecutionStoreError("Uso estruturado deve conter valores não negativos")
        updates: dict[str, object] = {}
        if tokens is not None:
            updates[f"{provider}_tokens"] = tokens
        if cost is not None:
            updates[f"{provider}_cost"] = cost
        return self.checkpoint(execution_id, summary=f"Uso estruturado de {provider} registrado", **updates)

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
    return _redact_secrets(value)[:_SUMMARY_LIMIT]


def _redact_secrets(value: str) -> str:
    return re.sub(
        r"(?i)(token|authorization|password|secret)\s*[:=]\s*\S+",
        r"\1=[redigido]",
        value.replace("\n", " "),
    )


def _sanitize_finding(value: str, limit: int) -> str:
    return _redact_secrets(value)[:limit]


def _record(row: sqlite3.Row) -> RunRecord:
    excluded = {"id", "issue_number", "phase", "created_at", "updated_at", "terminal"}
    values = {key: row[key] for key in row.keys() if key not in excluded}
    values["codex_start_attempted"] = bool(values.get("codex_start_attempted", False))
    for field in ("quota_observed_at", "quota_retry_at"):
        if values.get(field):
            values[field] = _parse_time(values[field])
    return RunRecord(
        row["id"],
        row["issue_number"],
        ExecutionPhase(row["phase"]),
        _parse_time(row["created_at"]),
        _parse_time(row["updated_at"]),
        **values,
    )
