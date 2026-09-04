"""Planejador puro e fail-closed para a retomada de execuções."""

from __future__ import annotations

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiState,
    MergeState,
    ProjectState,
    PullRequestObservation,
    PullRequestState,
    RecoveryAction,
    RecoveryDecision,
    RecoveryObservation,
    RecoveryPolicy,
    ReviewState,
    WorktreeState,
)


class RecoveryPlanner:
    """Decide sem I/O a partir do record, fatos observados e política explícita."""

    def __init__(self, policy: RecoveryPolicy) -> None:
        self.policy = policy

    def plan(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        return self._for_phase(run, observed)

    @staticmethod
    def _decision(action: RecoveryAction, reason: str, next_phase: ExecutionPhase | None = None) -> RecoveryDecision:
        return RecoveryDecision(action, reason, next_phase)

    def _block(self, reason: str) -> RecoveryDecision:
        return self._decision(RecoveryAction.BLOCK, reason)

    @staticmethod
    def _same_head(sha: str | None, expected: str | None) -> bool:
        return sha is not None and expected is not None and sha == expected

    def _valid_pull_request(self, run: RunRecord, pull_request: PullRequestObservation, head: str) -> bool:
        return (
            pull_request.state == PullRequestState.OPEN
            and pull_request.repository_full_name == self.policy.repository_full_name
            and pull_request.base == self.policy.pull_request_base
            and pull_request.head_branch == run.branch
            and pull_request.head_sha == head
            and (run.pull_request_number is None or pull_request.number == run.pull_request_number)
            and (run.pull_request_url is None or pull_request.url == run.pull_request_url)
        )

    def _single_valid_pull_request(self, run: RunRecord, observed: RecoveryObservation, head: str) -> PullRequestObservation | None:
        if len(observed.pull_requests) != 1:
            return None
        pull_request = observed.pull_requests[0]
        return pull_request if self._valid_pull_request(run, pull_request, head) else None

    @staticmethod
    def _has_worktree_identity(run: RunRecord) -> bool:
        return bool(run.branch and run.worktree_path and run.base_ref)

    @staticmethod
    def _has_complete_pr_identity(run: RunRecord) -> bool:
        return (run.pull_request_number is None) == (run.pull_request_url is None)

    def _has_convergent_persisted_pr(self, run: RunRecord, observed: RecoveryObservation) -> bool:
        return (
            run.pull_request_number is not None
            and run.pull_request_url is not None
            and run.current_head_sha is not None
            and self._single_valid_pull_request(run, observed, run.current_head_sha)
            is not None
        )

    def _for_phase(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        phase = run.phase
        if phase != ExecutionPhase.PROJECT_DONE_PENDING and not self._has_worktree_identity(run):
            return self._block("Identidade persistida do worktree está incompleta.")
        if not self._has_complete_pr_identity(run):
            return self._block("Identidade persistida do Pull Request está parcial.")
        if phase == ExecutionPhase.PREPARING:
            if observed.worktree_state == WorktreeState.DIVERGENT:
                return self._block("Worktree divergente do estado persistido.")
            if observed.worktree_state == WorktreeState.ABSENT:
                return self._decision(RecoveryAction.PREPARE_WORKTREE, "Worktree ainda não existe.")
            return self._decision(RecoveryAction.ADVANCE_PHASE, "Worktree já está convergente.", ExecutionPhase.CODEX_RUNNING)
        if observed.worktree_state != WorktreeState.CONVERGENT:
            return self._block("Worktree não está convergente para esta fase.")
        published_phases = {
            ExecutionPhase.PUSH_PENDING,
            ExecutionPhase.PR_PENDING,
            ExecutionPhase.WAITING_CI,
            ExecutionPhase.GEMINI_REVIEWING,
            ExecutionPhase.NEEDS_CHANGES,
            ExecutionPhase.MERGE_PENDING,
        }
        if phase in published_phases and (
            run.current_head_sha is None or observed.local_head_sha != run.current_head_sha
        ):
            return self._block("HEAD local diverge do checkpoint persistido.")
        pr_required_phases = {
            ExecutionPhase.WAITING_CI,
            ExecutionPhase.GEMINI_REVIEWING,
            ExecutionPhase.NEEDS_CHANGES,
            ExecutionPhase.MERGE_PENDING,
        }
        if phase in pr_required_phases and not self._has_convergent_persisted_pr(run, observed):
            return self._block("Pull Request persistido não converge com a observação.")
        if phase == ExecutionPhase.CODEX_RUNNING:
            return self._decision(RecoveryAction.RESUME_CODEX, "Sessão Codex persistida.") if run.codex_session_id else self._block("Sessão Codex ausente para retomada.")
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
            return self._project_done(run, observed)
        return self._block("Fase legada ou terminal não é planejável nesta rodada.")

    def _commit(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        checkpoint = run.current_head_sha
        if checkpoint is None:
            return self._block("HEAD de checkpoint ausente para commit.")
        if observed.has_worktree_changes and observed.local_head_sha == checkpoint:
            return self._decision(RecoveryAction.CREATE_COMMIT, "Alterações locais prontas para commit.")
        if not observed.has_worktree_changes and observed.local_head_sha not in {None, checkpoint} and observed.local_head_parent_sha == checkpoint:
            return self._decision(RecoveryAction.RECORD_EXISTING_COMMIT, "Commit direto já foi observado após o checkpoint.", ExecutionPhase.PUSH_PENDING)
        return self._block("Estado local não prova um commit direto e seguro.")

    def _push(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        local = observed.local_head_sha
        if local is None or local != run.current_head_sha:
            return self._block("HEAD local diverge do checkpoint de push.")
        if observed.remote_head_sha is None:
            return self._decision(RecoveryAction.PUSH_BRANCH, "Push ainda não foi observado.")
        if observed.remote_head_sha == local:
            return self._decision(RecoveryAction.RECORD_EXISTING_PUSH, "Push já foi observado.", ExecutionPhase.PR_PENDING)
        if observed.remote_head_sha == observed.local_head_parent_sha:
            return self._decision(RecoveryAction.PUSH_BRANCH, "Push ainda não foi observado.")
        return self._block("HEAD remoto não é o pai direto do HEAD local.")

    def _pull_request(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = run.current_head_sha
        if head is None or observed.remote_head_sha != head:
            return self._block("Branch remota não converge para o HEAD esperado.")
        if not observed.pull_requests:
            if run.pull_request_number is not None or run.pull_request_url is not None:
                return self._block("PR persistido não foi encontrado na observação.")
            return self._decision(RecoveryAction.CREATE_PULL_REQUEST, "Nenhum Pull Request foi observado.")
        if self._single_valid_pull_request(run, observed, head) is None:
            return self._block("Pull Request ambíguo ou com identidade divergente.")
        return self._decision(RecoveryAction.ADOPT_PULL_REQUEST, "Pull Request convergente já existe.", ExecutionPhase.WAITING_CI)

    def _ci(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = run.current_head_sha
        if head is None:
            return self._block("HEAD de checkpoint ausente para CI.")
        if observed.ci.state == CiState.ABSENT:
            return self._decision(RecoveryAction.WAIT_FOR_CI, "CI ainda não foi observada.") if observed.ci.head_sha is None else self._block("CI ausente não pode possuir HEAD associado.")
        if not self._same_head(observed.ci.head_sha, head):
            return self._block("CI observada para HEAD diferente.")
        if observed.ci.state == CiState.PENDING:
            return self._decision(RecoveryAction.WAIT_FOR_CI, "CI ainda não concluiu para o HEAD atual.")
        if observed.ci.state == CiState.SUCCESS:
            return self._decision(RecoveryAction.RECORD_CI_SUCCESS, "CI aprovada para o HEAD atual.", ExecutionPhase.GEMINI_REVIEWING)
        return self._block("CI falhou para o HEAD atual.")

    def _review(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        verdict, reviewed_head = run.review_verdict, run.reviewed_head_sha
        if verdict is None and reviewed_head is None:
            return self._decision(RecoveryAction.REVIEW_HEAD, "Review ainda não foi persistida.")
        if verdict is None or reviewed_head is None:
            return self._block("Review persistida parcialmente.")
        if reviewed_head != run.current_head_sha:
            return self._block("Review persistida para HEAD diferente.")
        try:
            state = ReviewState(verdict)
        except ValueError:
            return self._block("Veredito de review persistido é desconhecido.")
        if state == ReviewState.REJECTED:
            if observed.findings_head_sha != reviewed_head:
                return self._block("Findings persistidos ausentes para o HEAD rejeitado.")
            return self._decision(RecoveryAction.ADVANCE_PHASE, "Review rejeitada e findings persistidos.", ExecutionPhase.NEEDS_CHANGES)
        target = ExecutionPhase.MERGE_PENDING if self.policy.auto_merge_enabled else ExecutionPhase.APPROVED_AWAITING_ACTION
        return self._decision(RecoveryAction.ADVANCE_PHASE, "Review aprovada para o HEAD atual.", target)

    def _needs_changes(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        head = run.current_head_sha
        if not run.codex_session_id or run.review_verdict != ReviewState.REJECTED.value or run.reviewed_head_sha != head or observed.findings_head_sha != head:
            return self._block("Sessão, review e findings devem corresponder ao HEAD atual.")
        return self._decision(RecoveryAction.RESUME_CORRECTION, "Findings e sessão Codex correspondem ao HEAD atual.")

    def _merge(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        approved = run.reviewed_head_sha
        if approved is None or run.review_verdict != ReviewState.APPROVED.value:
            return self._block("Não há review aprovada persistida para merge.")
        if observed.local_head_sha != approved:
            return self._block("HEAD local diverge do HEAD aprovado.")
        if observed.merge.state == MergeState.UNKNOWN:
            return self._block("Estado de merge desconhecido.")
        if observed.merge.state == MergeState.MERGED:
            if observed.merge.merged_head_sha != approved or not observed.merge.merge_commit_sha:
                return self._block("Merge observado não corresponde ao HEAD aprovado.")
            return self._decision(RecoveryAction.RECORD_EXISTING_MERGE, "Merge já confirmado para o HEAD aprovado.", ExecutionPhase.PROJECT_DONE_PENDING)
        if observed.merge.state != MergeState.OPEN:
            return self._block("Merge não está aberto.")
        if self._single_valid_pull_request(run, observed, approved) is None or observed.ci.state != CiState.SUCCESS or observed.ci.head_sha != approved:
            return self._block("PR ou CI não converge para o HEAD aprovado.")
        return self._decision(RecoveryAction.MERGE_PULL_REQUEST, "PR aprovado e CI verde para o HEAD exato.")

    def _project_done(self, run: RunRecord, observed: RecoveryObservation) -> RecoveryDecision:
        if (
            not run.project_item_id
            or not run.reviewed_head_sha
            or not run.merged_head_sha
            or not run.merge_commit_sha
            or run.merged_head_sha != run.reviewed_head_sha
        ):
            return self._block("Merge persistido não está comprovado para atualizar o projeto.")
        if observed.project_state == ProjectState.UNKNOWN:
            return self._block("Estado do projeto é desconhecido.")
        if observed.project_state == ProjectState.NOT_DONE:
            return self._decision(RecoveryAction.MARK_PROJECT_DONE, "Item do projeto ainda não está Done.")
        return self._decision(RecoveryAction.COMPLETE, "Item do projeto já está Done.", ExecutionPhase.COMPLETED)
