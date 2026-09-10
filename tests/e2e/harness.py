"""Harness local para regressões E2E de recovery.

O estado remoto é deliberadamente pequeno, explícito e roteirizável.  Não há
rede, relógio real nem processos de provider: os testes exercitam SQLite,
planner, executor e serviço de retomada tal como usados em produção.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, ProjectState,
    PullRequestObservation, PullRequestState, RecoveryObservation, RecoveryPolicy,
    WorktreeState,
)
from ai_dev_orchestrator.domain.review import FindingSeverity, ReviewFinding, ReviewVerdict, StructuredReview
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import CommitResult, RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.services.resume import ResumeService

BASE = "a" * 40
HEAD_ONE = "b" * 40
HEAD_TWO = "c" * 40
MERGE = "d" * 40
PR_URL = "https://example.test/acme/repo/pull/1"


@dataclass
class LocalWorld:
    """Adapter fake declarativo; cada efeito altera somente fatos comprováveis."""

    initial_head: str = BASE
    local_head: str | None = None
    remote_head: str | None = None
    pr_exists: bool = False
    merged: bool = False
    done: bool = False
    reject_first_review: bool = True
    calls: list[str] = field(default_factory=list)
    review_prompts: list[str] = field(default_factory=list)
    crash_after: str | None = None
    _prepared: bool = False

    def _crash(self, boundary: str) -> None:
        if self.crash_after == boundary:
            self.crash_after = None
            raise RuntimeError(f"queda injetada após {boundary}")

    def prepare_worktree(self, run: RunRecord) -> str:
        self.calls.append("prepare")
        self._prepared, self.local_head = True, self.initial_head
        return self.local_head

    def start_codex(self, run: RunRecord) -> str:
        self.calls.append("start_codex")
        return "sessao-unica"

    def resume_codex(self, run: RunRecord) -> str:
        self.calls.append("resume_codex")
        return run.codex_session_id or ""

    def run_local_gates(self, run: RunRecord) -> None:
        self.calls.append("gates")

    def create_commit(self, run: RunRecord) -> CommitResult:
        self.calls.append("commit")
        next_head = HEAD_ONE if run.current_head_sha == BASE else HEAD_TWO
        self.local_head = next_head
        return CommitResult(next_head, run.current_head_sha or "")

    def push_branch(self, run: RunRecord) -> None:
        self.calls.append("push")
        self.remote_head = run.current_head_sha
        self._crash("push")

    def create_pull_request(self, run: RunRecord) -> PullRequestObservation:
        self.calls.append("create_pr")
        self.pr_exists = True
        self._crash("pr")
        return self.pull_request(run, PullRequestState.OPEN)

    def wait_for_ci(self, run: RunRecord) -> CiObservation:
        self.calls.append("ci")
        return CiObservation(CiState.SUCCESS, run.current_head_sha)

    def resume_ci_failure(self, run: RunRecord) -> str:
        self.calls.append("resume_ci_failure")
        return run.codex_session_id or ""

    def review_head(self, run: RunRecord, prior_findings: tuple[ReviewFinding, ...]) -> StructuredReview:
        self.calls.append("review")
        self.review_prompts.append("dossiê local " * 1_000)
        self._crash("review")
        if self.reject_first_review and run.current_head_sha == HEAD_ONE:
            finding = ReviewFinding(FindingSeverity.HIGH, "Falha reproduzível", "Corrija o caso", "src/x.py", 1)
            return StructuredReview(ReviewVerdict.REJECTED, (finding,), HEAD_ONE, "corrigir")
        return StructuredReview(ReviewVerdict.APPROVED, (), run.current_head_sha or "", "aprovado")

    def resume_correction(self, run: RunRecord, findings: tuple[ReviewFinding, ...]) -> str:
        self.calls.append("resume_correction")
        assert findings
        return run.codex_session_id or ""

    def merge_pull_request(self, run: RunRecord) -> MergeObservation:
        self.calls.append("merge")
        self.merged = True
        self._crash("merge")
        return MergeObservation(MergeState.MERGED, run.reviewed_head_sha, MERGE)

    def mark_project_done(self, run: RunRecord) -> None:
        self.calls.append("done")
        self.done = True

    def pull_request(self, run: RunRecord, state: PullRequestState) -> PullRequestObservation:
        return PullRequestObservation(
            1, PR_URL, "acme/repo", "main", run.branch or "", self.remote_head or "", state
        )


class WorldObserver:
    def __init__(self, world: LocalWorld) -> None:
        self.world = world

    def observe(self, run: RunRecord) -> RecoveryObservation:
        world = self.world
        worktree = WorktreeState.CONVERGENT if world._prepared else WorktreeState.ABSENT
        pulls: tuple[PullRequestObservation, ...] = ()
        if world.pr_exists:
            pulls = (world.pull_request(
                run, PullRequestState.MERGED if world.merged else PullRequestState.OPEN
            ),)
        ci = CiObservation(CiState.ABSENT)
        if world.pr_exists and run.current_head_sha == world.remote_head:
            ci = CiObservation(CiState.SUCCESS, world.remote_head)
        merge = MergeObservation(
            MergeState.MERGED if world.merged else MergeState.OPEN,
            run.reviewed_head_sha if world.merged else None,
            MERGE if world.merged else None,
        )
        findings_head = run.reviewed_head_sha if run.review_verdict == ReviewVerdict.REJECTED else None
        return RecoveryObservation(
            worktree, local_head_sha=world.local_head, remote_head_sha=world.remote_head,
            local_head_parent_sha=(BASE if world.local_head == HEAD_ONE else HEAD_ONE if world.local_head == HEAD_TWO else None),
            has_worktree_changes=run.phase == ExecutionPhase.COMMIT_PENDING,
            pull_requests=pulls, ci=ci, merge=merge,
            project_state=ProjectState.DONE if world.done else ProjectState.NOT_DONE,
            findings_head_sha=findings_head,
        )


def make_service(tmp_path: Path, world: LocalWorld, issue: int = 67) -> tuple[SqliteExecutionStore, ResumeService, RunRecord]:
    store = SqliteExecutionStore(tmp_path / f"e2e-{issue}.db")
    run = store.create(issue, project_item_id=f"item-{issue}", branch=f"work/issue-{issue}",
                       worktree_path=f"C:/e2e/{issue}", base_ref="main")
    policy = RecoveryPolicy("acme/repo", "main", True, 3)
    service = ResumeService(store, WorldObserver(world), RecoveryPlanner(policy), RecoveryExecutor(policy, store, world))
    return store, service, run


def assert_invariants(store: SqliteExecutionStore, issue: int) -> RunRecord:
    """Invariantes transversais, chamados por todo cenário que cria uma execução."""
    history = store.list_history(issue)
    assert len(history) == 1, "uma Issue não pode ganhar segunda execução durante recovery"
    run = history[0]
    assert run.branch and run.worktree_path and run.base_ref
    events = store.events(run.id)
    assert events and all("dossiê local" not in event.summary for event in events)
    assert all("Authorization=" not in event.summary for event in events)
    if run.phase == ExecutionPhase.COMPLETED:
        assert run.pull_request_number == 1
        assert run.merged_head_sha == run.reviewed_head_sha == run.ci_head_sha
        assert run.merge_commit_sha == MERGE
    if run.phase != ExecutionPhase.COMPLETED:
        assert run.project_status != "Done"
    return run
