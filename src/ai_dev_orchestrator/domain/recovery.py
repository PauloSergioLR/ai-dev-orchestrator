"""Fatos observados, política e decisões puras para a retomada segura."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ai_dev_orchestrator.domain.execution import ExecutionPhase


class WorktreeState(StrEnum):
    ABSENT = "ABSENT"
    CONVERGENT = "CONVERGENT"
    DIVERGENT = "DIVERGENT"


class PullRequestState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class CiState(StrEnum):
    ABSENT = "ABSENT"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class ReviewState(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MergeState(StrEnum):
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class ProjectState(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_DONE = "NOT_DONE"
    DONE = "DONE"


class RecoveryAction(StrEnum):
    """Uma única ação, ou um único checkpoint determinístico, por decisão."""

    PREPARE_WORKTREE = "PREPARE_WORKTREE"
    START_CODEX = "START_CODEX"
    RESUME_CODEX = "RESUME_CODEX"
    RUN_LOCAL_GATES = "RUN_LOCAL_GATES"
    CREATE_COMMIT = "CREATE_COMMIT"
    RECORD_EXISTING_COMMIT = "RECORD_EXISTING_COMMIT"
    PUSH_BRANCH = "PUSH_BRANCH"
    RECORD_EXISTING_PUSH = "RECORD_EXISTING_PUSH"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    ADOPT_PULL_REQUEST = "ADOPT_PULL_REQUEST"
    WAIT_FOR_CI = "WAIT_FOR_CI"
    RECORD_CI_SUCCESS = "RECORD_CI_SUCCESS"
    REVIEW_HEAD = "REVIEW_HEAD"
    RESUME_CORRECTION = "RESUME_CORRECTION"
    MERGE_PULL_REQUEST = "MERGE_PULL_REQUEST"
    RECORD_EXISTING_MERGE = "RECORD_EXISTING_MERGE"
    MARK_PROJECT_DONE = "MARK_PROJECT_DONE"
    COMPLETE = "COMPLETE"
    ADVANCE_PHASE = "ADVANCE_PHASE"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class RecoveryPolicy:
    """Configuração e invariantes esperadas, distintas dos fatos observados."""

    repository_full_name: str
    pull_request_base: str
    auto_merge_enabled: bool
    max_correction_attempts: int
    done_status: str = "Done"

    def __post_init__(self) -> None:
        if self.max_correction_attempts <= 0:
            raise ValueError("max_correction_attempts deve ser maior que zero")
        if not self.done_status:
            raise ValueError("done_status não pode ser vazio")


@dataclass(frozen=True)
class PullRequestObservation:
    number: int
    url: str
    repository_full_name: str
    base: str
    head_branch: str
    head_sha: str
    state: PullRequestState


@dataclass(frozen=True)
class CiObservation:
    state: CiState = CiState.ABSENT
    head_sha: str | None = None


@dataclass(frozen=True)
class MergeObservation:
    state: MergeState = MergeState.UNKNOWN
    merged_head_sha: str | None = None
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class RecoveryObservation:
    """Snapshot normalizado coletado exclusivamente pelos adapters de observação."""

    worktree_state: WorktreeState
    local_head_sha: str | None = None
    local_head_parent_sha: str | None = None
    has_worktree_changes: bool = False
    remote_head_sha: str | None = None
    pull_requests: tuple[PullRequestObservation, ...] = ()
    ci: CiObservation = field(default_factory=CiObservation)
    findings_head_sha: str | None = None
    merge: MergeObservation = field(default_factory=MergeObservation)
    project_state: ProjectState = ProjectState.UNKNOWN


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    next_phase: ExecutionPhase | None = None
