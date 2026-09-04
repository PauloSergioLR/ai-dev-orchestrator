"""Aplicação única e auditável de decisões já tomadas pelo planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiState, MergeObservation, MergeState, PullRequestObservation, PullRequestState,
    RecoveryAction, RecoveryDecision, RecoveryObservation, RecoveryPolicy,
)
from ai_dev_orchestrator.domain.review import ReviewFinding, StructuredReview
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


class RecoveryExecutionError(Exception):
    """A decisão não pode ser aplicada com segurança."""


@dataclass(frozen=True)
class CommitResult:
    new_head_sha: str
    parent_head_sha: str


class RecoveryEffects(Protocol):
    def prepare_worktree(self, run: RunRecord) -> str: ...
    def start_codex(self, run: RunRecord) -> str: ...
    def resume_codex(self, run: RunRecord) -> str: ...
    def run_local_gates(self, run: RunRecord) -> None: ...
    def create_commit(self, run: RunRecord) -> CommitResult: ...
    def push_branch(self, run: RunRecord) -> None: ...
    def create_pull_request(self, run: RunRecord) -> PullRequestObservation: ...
    def wait_for_ci(self, run: RunRecord): ...
    def review_head(self, run: RunRecord, prior_findings: tuple[ReviewFinding, ...]) -> StructuredReview: ...
    def resume_correction(self, run: RunRecord, findings: tuple[ReviewFinding, ...]) -> str: ...
    def merge_pull_request(self, run: RunRecord) -> MergeObservation: ...
    def mark_project_done(self, run: RunRecord) -> None: ...


class RecoveryExecutor:
    def __init__(self, policy: RecoveryPolicy, store: SqliteExecutionStore, effects: RecoveryEffects) -> None:
        self.policy, self.store, self.effects = policy, store, effects

    def execute(self, run: RunRecord, decision: RecoveryDecision, observation: RecoveryObservation) -> RunRecord:
        current = self.store.get(run.id)
        if current != run:
            raise RecoveryExecutionError("Execução mudou desde o planejamento")
        if decision.action == RecoveryAction.BLOCK:
            raise RecoveryExecutionError(decision.reason)
        action = decision.action
        if action == RecoveryAction.PREPARE_WORKTREE:
            head = self.effects.prepare_worktree(run)
            self._required(head, "HEAD inicial")
            return self.store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary=decision.reason, current_head_sha=head, head_sha=head)
        if action == RecoveryAction.ADVANCE_PHASE:
            if decision.next_phase is None:
                raise RecoveryExecutionError("ADVANCE_PHASE exige próxima fase")
            updates: dict[str, object] = {}
            if run.phase == ExecutionPhase.PREPARING and decision.next_phase == ExecutionPhase.CODEX_RUNNING:
                self._required(observation.local_head_sha, "HEAD local")
                updates["current_head_sha"] = observation.local_head_sha
            return self.store.transition(run.id, decision.next_phase, summary=decision.reason, **updates)
        if action == RecoveryAction.START_CODEX:
            if run.codex_session_id:
                raise RecoveryExecutionError("Sessão Codex já existe")
            session = self.effects.start_codex(run)
            self._required(session, "Sessão Codex")
            return self.store.transition(run.id, ExecutionPhase.TESTING, summary=decision.reason, codex_session_id=session)
        if action == RecoveryAction.RESUME_CODEX:
            self._required(run.codex_session_id, "Sessão Codex")
            if self.effects.resume_codex(run) != run.codex_session_id:
                raise RecoveryExecutionError("Provider retornou sessão Codex divergente")
            return self.store.transition(run.id, ExecutionPhase.TESTING, summary=decision.reason)
        if action == RecoveryAction.RUN_LOCAL_GATES:
            self.effects.run_local_gates(run)
            return self.store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary=decision.reason)
        if action in {RecoveryAction.CREATE_COMMIT, RecoveryAction.RECORD_EXISTING_COMMIT}:
            result = self.effects.create_commit(run) if action == RecoveryAction.CREATE_COMMIT else CommitResult(observation.local_head_sha or "", observation.local_head_parent_sha or "")
            if not result.new_head_sha or result.new_head_sha == run.current_head_sha or result.parent_head_sha != run.current_head_sha:
                raise RecoveryExecutionError("Commit não prova relação direta com o checkpoint")
            return self.store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary=decision.reason, current_head_sha=result.new_head_sha, ci_head_sha=None, reviewed_head_sha=None, review_verdict=None, merge_commit_sha=None, merged_head_sha=None, head_sha=result.new_head_sha)
        if action == RecoveryAction.PUSH_BRANCH:
            self.effects.push_branch(run)
            return self.store.transition(run.id, ExecutionPhase.PR_PENDING, summary=decision.reason)
        if action == RecoveryAction.RECORD_EXISTING_PUSH:
            return self.store.transition(run.id, ExecutionPhase.PR_PENDING, summary=decision.reason)
        if action in {RecoveryAction.CREATE_PULL_REQUEST, RecoveryAction.ADOPT_PULL_REQUEST}:
            pr = self.effects.create_pull_request(run) if action == RecoveryAction.CREATE_PULL_REQUEST else self._observed_pr(run, observation)
            self._validate_pr(run, pr)
            return self.store.transition(run.id, ExecutionPhase.WAITING_CI, summary=decision.reason, pull_request_number=pr.number, pull_request_url=pr.url)
        if action in {RecoveryAction.WAIT_FOR_CI, RecoveryAction.RECORD_CI_SUCCESS}:
            ci = self.effects.wait_for_ci(run) if action == RecoveryAction.WAIT_FOR_CI else observation.ci
            if ci.state in {CiState.ABSENT, CiState.PENDING} and action == RecoveryAction.WAIT_FOR_CI:
                return self.store.checkpoint(run.id, summary=decision.reason)
            if ci.state != CiState.SUCCESS or ci.head_sha != run.current_head_sha:
                raise RecoveryExecutionError("CI não confirmou o HEAD atual")
            return self.store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary=decision.reason, ci_head_sha=ci.head_sha)
        if action == RecoveryAction.REVIEW_HEAD:
            review = self.effects.review_head(run, self.store.review_findings(run.id))
            if review.reviewed_head_sha != run.current_head_sha:
                raise RecoveryExecutionError("Review retornou HEAD divergente")
            return self.store.record_review(run.id, review, decision.reason)
        if action == RecoveryAction.RESUME_CORRECTION:
            if run.correction_attempts >= self.policy.max_correction_attempts:
                raise RecoveryExecutionError("Limite de correções atingido")
            findings = self.store.review_findings(run.id, run.reviewed_head_sha)
            if not findings or not run.codex_session_id:
                raise RecoveryExecutionError("Findings ou sessão Codex ausentes")
            audited = self.store.checkpoint(run.id, summary="Tentativa de correção iniciada", correction_attempts=run.correction_attempts + 1)
            if self.effects.resume_correction(audited, findings) != run.codex_session_id:
                raise RecoveryExecutionError("Provider retornou sessão Codex divergente")
            return self.store.transition(run.id, ExecutionPhase.TESTING, summary=decision.reason)
        if action in {RecoveryAction.MERGE_PULL_REQUEST, RecoveryAction.RECORD_EXISTING_MERGE}:
            merge = self.effects.merge_pull_request(run) if action == RecoveryAction.MERGE_PULL_REQUEST else observation.merge
            if merge.state != MergeState.MERGED or merge.merged_head_sha != run.reviewed_head_sha or not merge.merge_commit_sha:
                raise RecoveryExecutionError("Merge não foi comprovado para o HEAD aprovado")
            return self.store.transition(run.id, ExecutionPhase.PROJECT_DONE_PENDING, summary=decision.reason, merged_head_sha=merge.merged_head_sha, merge_commit_sha=merge.merge_commit_sha)
        if action == RecoveryAction.MARK_PROJECT_DONE:
            self.effects.mark_project_done(run)
            return self.store.transition(run.id, ExecutionPhase.COMPLETED, summary=decision.reason, project_status="Done")
        if action == RecoveryAction.COMPLETE:
            return self.store.transition(run.id, ExecutionPhase.COMPLETED, summary=decision.reason)
        raise RecoveryExecutionError("Ação de recovery desconhecida")

    @staticmethod
    def _required(value: str | None, name: str) -> None:
        if not value:
            raise RecoveryExecutionError(f"{name} ausente")

    def _validate_pr(self, run: RunRecord, pr: PullRequestObservation) -> None:
        if pr.state != PullRequestState.OPEN or pr.repository_full_name != self.policy.repository_full_name or pr.base != self.policy.pull_request_base or pr.head_branch != run.branch or pr.head_sha != run.current_head_sha:
            raise RecoveryExecutionError("Pull Request retornado diverge da identidade esperada")

    @staticmethod
    def _observed_pr(run: RunRecord, observation: RecoveryObservation) -> PullRequestObservation:
        if len(observation.pull_requests) != 1:
            raise RecoveryExecutionError("Pull Request observado é ambíguo")
        return observation.pull_requests[0]
