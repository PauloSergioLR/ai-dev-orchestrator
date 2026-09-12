"""Diagnóstico local, estruturado e estritamente somente leitura de execuções."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ai_dev_orchestrator.domain.execution import TERMINAL_PHASES, ExecutionEvent, RunRecord
from ai_dev_orchestrator.domain.review import ReviewFinding
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore, sanitize_diagnostic_text


@dataclass(frozen=True)
class Inspection:
    """Contrato estável da saída JSON do comando ``orch inspect``."""

    issue: int
    execution_id: str
    phase: str
    terminal: bool
    branch: str | None
    worktree_path: str | None
    base_ref: str | None
    codex_session_id: str | None
    models: dict[str, str]
    pull_request: dict[str, Any]
    heads: dict[str, str | None]
    review: dict[str, Any]
    quota: dict[str, str | None]
    human_required: dict[str, str | None]
    last_error: str | None
    project_status: str | None
    cleanup: dict[str, str | None]
    repository_identity: str | None
    contract: dict[str, Any]
    corrections: dict[str, int]
    gates: tuple[dict[str, Any], ...]
    ci_checks: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    inconsistencies: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class InspectService:
    """Lê evidências locais sem iniciar providers, Git ou qualquer atualização."""

    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store

    @classmethod
    def from_database(cls, database_path) -> "InspectService":
        return cls(SqliteExecutionStore(database_path, read_only=True))

    def inspect(self, issue: int) -> Inspection | None:
        record = self.store.get_latest_for_issue(issue)
        if record is None:
            return None
        # Sem HEAD atual não há como afirmar que um finding pertence ao estado
        # que está sendo diagnosticado; nunca misturamos evidências históricas.
        findings = (
            self.store.review_findings(record.id, record.current_head_sha)
            if record.current_head_sha
            else ()
        )
        events = self.store.events(record.id)
        terminal = self.store.terminal_flag(record.id)
        return Inspection(
            issue=record.issue_number,
            execution_id=record.id,
            phase=record.phase.value,
            terminal=terminal,
            branch=record.branch,
            worktree_path=record.worktree_path,
            base_ref=record.base_ref,
            codex_session_id=record.codex_session_id,
            models={"codex": record.codex_model, "gemini": record.gemini_model},
            pull_request={"number": record.pull_request_number, "url": _safe(record.pull_request_url)},
            heads={"current": record.current_head_sha, "ci": record.ci_head_sha,
                   "reviewed": record.reviewed_head_sha, "merged": record.merged_head_sha,
                   "merge_commit": record.merge_commit_sha},
            review={"verdict": record.review_verdict, "correction_attempts": record.correction_attempts},
            quota={"provider": record.quota_provider, "classification": record.quota_classification,
                   "observed_at": _timestamp(record.quota_observed_at), "retry_at": _timestamp(record.quota_retry_at)},
            human_required={"reason": _safe(record.human_reason), "phase": record.human_phase,
                            "at": record.human_at},
            last_error=_safe(record.last_error),
            project_status=_safe(record.project_status),
            cleanup={"status": record.cleanup_status, "detail": _safe(record.cleanup_detail)},
            repository_identity=record.repository_identity,
            contract={"fingerprint": record.contract_fingerprint},
            corrections={
                "local_gates": record.local_gate_correction_attempts,
                "ci": record.ci_correction_attempts,
                "review": record.correction_attempts,
            },
            gates=_gate_results(record.gate_results_json),
            ci_checks=_gate_results(record.ci_checks_json),
            findings=tuple(_finding(finding) for finding in findings),
            events=tuple(_event(event) for event in events[-10:]),
            inconsistencies=tuple(_inconsistencies(record, terminal, findings)),
        )


def _gate_results(value: str | None) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    try:
        payload = __import__("json").loads(value)
    except (ValueError, TypeError):
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(
        {key: _safe(value) if isinstance(value, str) else value for key, value in item.items()}
        for item in payload
        if isinstance(item, dict)
    )


def _safe(value: str | None) -> str | None:
    return sanitize_diagnostic_text(value)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _finding(finding: ReviewFinding) -> dict[str, Any]:
    return {"severity": finding.severity.value, "title": _safe(finding.title), "path": _safe(finding.path),
            "line": finding.line, "criterion": _safe(finding.criterion)}


def _event(event: ExecutionEvent) -> dict[str, Any]:
    return {"sequence": event.sequence, "previous_phase": event.previous_phase.value if event.previous_phase else None,
            "phase": event.phase.value, "created_at": event.created_at.isoformat(),
            "summary": _safe(event.summary), "head_sha": event.head_sha}


def _inconsistencies(record: RunRecord, terminal: bool, findings: tuple[ReviewFinding, ...]) -> list[str]:
    messages: list[str] = []
    if terminal != (record.phase in TERMINAL_PHASES):
        messages.append("terminal diverge da terminalidade da fase")
    if (record.pull_request_number is None) != (record.pull_request_url is None):
        messages.append("número e URL do PR estão parcialmente persistidos")
    if record.review_verdict and not record.reviewed_head_sha:
        messages.append("veredito de review sem HEAD revisado")
    if record.reviewed_head_sha and not record.current_head_sha:
        messages.append("HEAD revisado sem HEAD atual")
    if findings and record.reviewed_head_sha != record.current_head_sha:
        messages.append("findings do HEAD atual divergem do HEAD revisado")
    quota_fields = (record.quota_provider, record.quota_classification, record.quota_observed_at)
    if any(quota_fields) and not all(quota_fields):
        messages.append("evidência de quota está parcialmente persistida")
    if record.phase.value == "HUMAN_REQUIRED" and not record.human_reason:
        messages.append("HUMAN_REQUIRED sem classificação humana")
    if record.human_reason and record.phase.value != "HUMAN_REQUIRED":
        messages.append("classificação humana persistida fora de HUMAN_REQUIRED")
    if record.merge_commit_sha and not record.merged_head_sha:
        messages.append("merge commit sem HEAD mesclado")
    return messages
