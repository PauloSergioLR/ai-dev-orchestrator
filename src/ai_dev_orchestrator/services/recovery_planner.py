"""Planejador puro e fail-closed para a retomada de execuções."""

from __future__ import annotations

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiState,
    MergeState,
    RecoveryAction,
    RecoveryDecision,
    RecoveryObservation,
    PullRequestState,
    ReviewState,
    WorktreeState,
)


class RecoveryPlanner:
    """Decide sem executar efeitos, usando somente um record e um snapshot."""

    def plan(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        return self._for_phase(run, observed)

    @staticmethod
    def _decision(
        action: RecoveryAction, reason: str, next_phase: ExecutionPhase | None = None
    ) -> RecoveryDecision:
        return RecoveryDecision(action, reason, next_phase)

    def _block(self, reason: str) -> RecoveryDecision:
        return self._decision(RecoveryAction.BLOCK, reason)

    def _head(self, run: RunRecord, observed: RecoveryObservation) -> str | None:
        return run.current_head_sha or observed.local_head_sha

    def _same_head(self, sha: str | None, expected: str | None) -> bool:
        return sha is not None and expected is not None and sha == expected

    def _for_phase(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        phase = run.phase
        if phase == ExecutionPhase.PREPARING:
            if observed.worktree_state == WorktreeState.DIVERGENT:
                return self._block("Worktree divergente do estado persistido.")
            if observed.worktree_state == WorktreeState.ABSENT:
                return self._decision(RecoveryAction.PREPARE_WORKTREE, "Worktree ainda não existe.")
            return self._decision(
                RecoveryAction.ADVANCE_PHASE,
                "Worktree já está convergente.",
                ExecutionPhase.CODEX_RUNNING,
            )

        if observed.worktree_state != WorktreeState.CONVERGENT:
            return self._block("Worktree não está convergente para esta fase.")

        if phase == ExecutionPhase.CODEX_RUNNING:
            if not run.codex_session_id:
                return self._block("Sessão Codex ausente para retomada.")
            return self._decision(RecoveryAction.RESUME_CODEX, "Sessão Codex persistida.")

        if phase == ExecutionPhase.TESTING:
            return self._decision(RecoveryAction.RUN_LOCAL_GATES, "Gates locais pendentes.")

        if phase == ExecutionPhase.COMMIT_PENDING:
            return self._commit(run, observed)
        if phase == ExecutionPhase.PUSH_PENDING:
            return self._push(run, observed)
        if phase == ExecutionPhase.PR_PENDING:
            return self._pull_request(run, observed)
        if phase == ExecutionPhase.WAITING_CI:
            return self._ci(run, observed)
        if phase == ExecutionPhase.GEMINI_REVIEWING:
            return self._review(run, observed)
        if phase == ExecutionPhase.NEEDS_CHANGES:
            return self._needs_changes(run, observed)
        if phase == ExecutionPhase.MERGE_PENDING:
            return self._merge(run, observed)
        if phase == ExecutionPhase.PROJECT_DONE_PENDING:
            action = RecoveryAction.COMPLETE if observed.project_done else RecoveryAction.MARK_PROJECT_DONE
            reason = "Item do projeto já está Done." if observed.project_done else "Item do projeto ainda não está Done."
            return self._decision(action, reason, ExecutionPhase.COMPLETED if observed.project_done else None)
        return self._block("Fase legada ou terminal não é planejável nesta rodada.")

    def _commit(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        expected = run.current_head_sha
        if expected is None:
            return self._block("HEAD de checkpoint ausente para commit.")
        if observed.has_local_changes and not observed.has_untracked_files and observed.local_head_sha == expected:
            return self._decision(RecoveryAction.CREATE_COMMIT, "Alterações locais prontas para commit.")
        if (
            not observed.has_local_changes
            and not observed.has_untracked_files
            and observed.local_head_sha not in {None, expected}
            and observed.local_head_descends_from_checkpoint
        ):
            return self._decision(RecoveryAction.RECORD_EXISTING_COMMIT, "Commit já observado após o checkpoint.", ExecutionPhase.PUSH_PENDING)
        return self._block("Estado local não prova um commit seguro.")

    def _push(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        local = observed.local_head_sha
        if local is None or (run.current_head_sha is not None and local != run.current_head_sha):
            return self._block("HEAD local diverge do checkpoint de push.")
        if observed.remote_head_sha == local:
            return self._decision(RecoveryAction.RECORD_EXISTING_PUSH, "Push já foi observado.", ExecutionPhase.PR_PENDING)
        if observed.remote_head_sha is None or observed.remote_head_is_ancestor_of_local:
            return self._decision(RecoveryAction.PUSH_BRANCH, "Push ainda não foi observado.")
        return self._block("HEAD remoto diverge do HEAD local.")

    def _pull_request(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = self._head(run, observed)
        if head is None or observed.remote_head_sha != head:
            return self._block("Branch remota não converge para o HEAD esperado.")
        if len(observed.pull_requests) == 0:
            return self._decision(RecoveryAction.CREATE_PULL_REQUEST, "Nenhum Pull Request foi observado.")
        if (
            len(observed.pull_requests) != 1
            or observed.pull_requests[0].head_sha != head
            or observed.pull_requests[0].state != PullRequestState.OPEN
        ):
            return self._block("Pull Request ambíguo ou divergente.")
        return self._decision(RecoveryAction.ADOPT_PULL_REQUEST, "Pull Request convergente já existe.", ExecutionPhase.WAITING_CI)

    def _ci(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = self._head(run, observed)
        if not self._same_head(observed.ci.head_sha, head) and observed.ci.state != CiState.ABSENT:
            return self._block("CI observada para HEAD diferente.")
        if observed.ci.state in {CiState.ABSENT, CiState.PENDING}:
            return self._decision(RecoveryAction.WAIT_FOR_CI, "CI ainda não concluiu para o HEAD atual.")
        if observed.ci.state == CiState.SUCCESS:
            return self._decision(RecoveryAction.RECORD_CI_SUCCESS, "CI aprovada para o HEAD atual.", ExecutionPhase.GEMINI_REVIEWING)
        return self._block("CI falhou para o HEAD atual.")

    def _review(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = self._head(run, observed)
        if observed.review.state == ReviewState.ABSENT:
            return self._decision(RecoveryAction.REVIEW_HEAD, "HEAD atual ainda não foi revisado.")
        if not self._same_head(observed.review.head_sha, head):
            return self._block("Review observada para HEAD diferente.")
        if observed.review.state == ReviewState.REJECTED:
            return self._decision(RecoveryAction.ADVANCE_PHASE, "Review rejeitada persistida.", ExecutionPhase.NEEDS_CHANGES)
        target = ExecutionPhase.MERGE_PENDING if observed.auto_merge_enabled else ExecutionPhase.APPROVED_AWAITING_ACTION
        return self._decision(RecoveryAction.ADVANCE_PHASE, "Review aprovada para o HEAD atual.", target)

    def _needs_changes(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = self._head(run, observed)
        if not run.codex_session_id:
            return self._block("Sessão Codex ausente para corrigir findings.")
        if observed.review.state != ReviewState.REJECTED or not self._same_head(observed.review.head_sha, head):
            return self._block("Review rejeitada não corresponde ao HEAD atual.")
        if not self._same_head(observed.findings_head_sha, head):
            return self._block("Findings persistidos ausentes para o HEAD rejeitado.")
        return self._decision(RecoveryAction.RESUME_CORRECTION, "Findings e sessão Codex correspondem ao HEAD atual.")

    def _merge(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        approved = run.reviewed_head_sha
        if approved is None or run.review_verdict != ReviewState.APPROVED.value:
            return self._block("Não há review aprovada persistida para merge.")
        if observed.merge.state == MergeState.MERGED:
            if observed.merge.merged_head_sha != approved or not observed.merge.merge_commit_sha:
                return self._block("Merge observado não corresponde ao HEAD aprovado.")
            return self._decision(RecoveryAction.RECORD_EXISTING_MERGE, "Merge já confirmado para o HEAD aprovado.", ExecutionPhase.PROJECT_DONE_PENDING)
        if (len(observed.pull_requests) != 1 or observed.pull_requests[0].head_sha != approved
                or observed.pull_requests[0].state != PullRequestState.OPEN
                or observed.ci.state != CiState.SUCCESS or observed.ci.head_sha != approved
                or observed.review.state != ReviewState.APPROVED or observed.review.head_sha != approved):
            return self._block("PR, CI ou review não converge para o HEAD aprovado.")
        return self._decision(RecoveryAction.MERGE_PULL_REQUEST, "PR aprovado e CI verde para o HEAD exato.")
