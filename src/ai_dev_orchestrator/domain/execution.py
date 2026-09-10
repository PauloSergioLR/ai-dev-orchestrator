"""Modelo durável e validado de uma execução do orquestrador."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ExecutionPhase(StrEnum):
    PREPARING = "PREPARING"
    CODEX_RUNNING = "CODEX_RUNNING"
    TESTING = "TESTING"
    COMMIT_PENDING = "COMMIT_PENDING"
    PUSH_PENDING = "PUSH_PENDING"
    PR_PENDING = "PR_PENDING"
    PUBLISHING = "PUBLISHING"
    WAITING_CI = "WAITING_CI"
    GEMINI_REVIEWING = "GEMINI_REVIEWING"
    WAITING_CODEX_QUOTA = "WAITING_CODEX_QUOTA"
    WAITING_GEMINI_QUOTA = "WAITING_GEMINI_QUOTA"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    MERGE_PENDING = "MERGE_PENDING"
    MERGING = "MERGING"
    PROJECT_DONE_PENDING = "PROJECT_DONE_PENDING"
    APPROVED_AWAITING_ACTION = "APPROVED_AWAITING_ACTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL_PHASES = frozenset(
    {
        ExecutionPhase.COMPLETED,
        ExecutionPhase.FAILED,
        ExecutionPhase.SUPERSEDED,
        ExecutionPhase.APPROVED_AWAITING_ACTION,
    }
)
_ALLOWED = {
    ExecutionPhase.PREPARING: {ExecutionPhase.CODEX_RUNNING, ExecutionPhase.FAILED},
    ExecutionPhase.CODEX_RUNNING: {
        ExecutionPhase.TESTING, ExecutionPhase.WAITING_CODEX_QUOTA, ExecutionPhase.FAILED,
    },
    ExecutionPhase.TESTING: {
        ExecutionPhase.COMMIT_PENDING,
        ExecutionPhase.PUBLISHING,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.COMMIT_PENDING: {ExecutionPhase.PUSH_PENDING, ExecutionPhase.FAILED},
    ExecutionPhase.PUSH_PENDING: {ExecutionPhase.PR_PENDING, ExecutionPhase.FAILED},
    ExecutionPhase.PR_PENDING: {ExecutionPhase.WAITING_CI, ExecutionPhase.FAILED},
    ExecutionPhase.PUBLISHING: {
        ExecutionPhase.PUSH_PENDING,
        ExecutionPhase.PR_PENDING,
        ExecutionPhase.WAITING_CI,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.WAITING_CI: {
        ExecutionPhase.CODEX_RUNNING,
        ExecutionPhase.GEMINI_REVIEWING,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.GEMINI_REVIEWING: {
        ExecutionPhase.WAITING_GEMINI_QUOTA,
        ExecutionPhase.NEEDS_CHANGES,
        ExecutionPhase.MERGE_PENDING,
        ExecutionPhase.MERGING,
        ExecutionPhase.APPROVED_AWAITING_ACTION,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.NEEDS_CHANGES: {
        ExecutionPhase.CODEX_RUNNING,
        ExecutionPhase.TESTING,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.WAITING_CODEX_QUOTA: {
        ExecutionPhase.CODEX_RUNNING, ExecutionPhase.FAILED,
    },
    ExecutionPhase.WAITING_GEMINI_QUOTA: {
        ExecutionPhase.GEMINI_REVIEWING, ExecutionPhase.FAILED,
    },
    ExecutionPhase.MERGE_PENDING: {
        ExecutionPhase.PROJECT_DONE_PENDING,
        ExecutionPhase.FAILED,
    },
    ExecutionPhase.PROJECT_DONE_PENDING: {ExecutionPhase.COMPLETED, ExecutionPhase.FAILED},
    ExecutionPhase.MERGING: {
        ExecutionPhase.PROJECT_DONE_PENDING,
        ExecutionPhase.COMPLETED,
        ExecutionPhase.FAILED,
    },
}

# Esperas suspendem a fase; nunca apagam o checkpoint de retomada.
PROVIDER_WAIT_PHASES = frozenset({
    ExecutionPhase.WAITING_CODEX_QUOTA, ExecutionPhase.WAITING_GEMINI_QUOTA,
    ExecutionPhase.WAITING_PROVIDER, ExecutionPhase.BLOCKED_PROVIDER,
})
RESUMABLE_PROVIDER_PHASES = frozenset({
    ExecutionPhase.CODEX_RUNNING, ExecutionPhase.GEMINI_REVIEWING,
    ExecutionPhase.TESTING, ExecutionPhase.NEEDS_CHANGES,
})
for _phase in RESUMABLE_PROVIDER_PHASES:
    _ALLOWED[_phase].update(PROVIDER_WAIT_PHASES)
for _phase in PROVIDER_WAIT_PHASES:
    _ALLOWED.setdefault(_phase, set()).update(RESUMABLE_PROVIDER_PHASES)
_ALLOWED[ExecutionPhase.HUMAN_REQUIRED] = set(RESUMABLE_PROVIDER_PHASES)


def validate_transition(old: ExecutionPhase, new: ExecutionPhase) -> None:
    """Recusa saltos e reaberturas que ocultariam o histórico da execução."""
    if new not in _ALLOWED.get(old, set()):
        raise ValueError(f"Transição de execução inválida: {old} -> {new}")


@dataclass(frozen=True)
class RunRecord:
    id: str
    issue_number: int
    phase: ExecutionPhase
    created_at: datetime
    updated_at: datetime
    project_item_id: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    base_ref: str | None = None
    codex_session_id: str | None = None
    codex_start_attempted: bool = False
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    current_head_sha: str | None = None
    ci_head_sha: str | None = None
    reviewed_head_sha: str | None = None
    review_verdict: str | None = None
    correction_attempts: int = 0
    merge_commit_sha: str | None = None
    merged_head_sha: str | None = None
    project_status: str | None = None
    human_reason: str | None = None
    human_phase: str | None = None
    human_at: str | None = None
    last_error: str | None = None
    codex_model: str = "default"
    gemini_model: str = "default"
    quota_provider: str | None = None
    quota_classification: str | None = None
    quota_observed_at: datetime | None = None
    quota_retry_at: datetime | None = None
    provider_resume_phase: str | None = None
    provider_retry_attempts: int = 0
    cleanup_status: str = "PENDING"
    cleanup_detail: str | None = None
    codex_tokens: int | None = None
    gemini_tokens: int | None = None
    codex_cost: float | None = None
    gemini_cost: float | None = None


@dataclass(frozen=True)
class ExecutionEvent:
    execution_id: str
    sequence: int
    previous_phase: ExecutionPhase | None
    phase: ExecutionPhase
    created_at: datetime
    summary: str
    head_sha: str | None = None


class ExecutionStore(Protocol):
    """Porta de persistência usada pelo pipeline, independente do SQLite."""

    def create(self, issue_number: int, **details: object) -> RunRecord: ...

    def get(self, execution_id: str) -> RunRecord: ...

    def transition(
        self,
        execution_id: str,
        phase: ExecutionPhase,
        *,
        summary: str,
        head_sha: str | None = None,
        **updates: object,
    ) -> RunRecord: ...

    def checkpoint(
        self,
        execution_id: str,
        *,
        summary: str,
        head_sha: str | None = None,
        **updates: object,
    ) -> RunRecord: ...

    def list_history(self, issue_number: int | None = None) -> tuple[RunRecord, ...]: ...

    def require_human(self, execution_id: str, *, summary: str, reason: str) -> RunRecord: ...

    def claim_notification(self, execution_id: str, event_key: str, channel: str,
                           *, max_attempts: int, retry_seconds: float) -> bool: ...

    def finish_notification(self, execution_id: str, event_key: str, channel: str,
                            *, sent: bool) -> None: ...
