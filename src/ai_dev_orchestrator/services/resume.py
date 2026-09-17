"""Orquestra uma retomada segura sem escolher ações fora do planner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from ai_dev_orchestrator.domain.execution import RunRecord, TERMINAL_PHASES, PROVIDER_WAIT_PHASES
from ai_dev_orchestrator.domain.recovery import (
    MergeState,
    PullRequestState,
    RecoveryAction,
    RecoveryObservation,
)
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.provider import ProviderFailure
from datetime import datetime
from ai_dev_orchestrator.services.provider_recovery import (
    record_provider_failure, resume_provider_wait, ProviderRecoveryError,
)


class ResumeError(Exception):
    """A retomada não pode continuar com segurança."""


class RecoveryObserver(Protocol):
    def observe(self, run: RunRecord) -> RecoveryObservation: ...


@dataclass(frozen=True)
class ResumeResult:
    issue_number: int
    execution_id: str
    phase: str
    branch: str | None
    codex_session_id: str | None
    pull_request_number: int | None
    current_head_sha: str | None
    correction_attempts: int
    pull_request_url: str | None = None
    ci_status: str | None = None
    review_verdict: str | None = None
    merge_status: str = "NOT_REQUESTED"
    project_status: str | None = None
    quota_retry_at: datetime | None = None


class ResumeService:
    def __init__(self, store: SqliteExecutionStore, observer: RecoveryObserver,
                 planner: RecoveryPlanner, executor: RecoveryExecutor,
                 codex_model: str | None = None, gemini_model: str | None = None, escalation=None) -> None:
        self.store, self.observer, self.planner, self.executor = store, observer, planner, executor
        self.codex_model, self.gemini_model = codex_model, gemini_model
        self.escalation = escalation

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "ResumeService":
        from ai_dev_orchestrator.domain.recovery import RecoveryPolicy
        from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
        from ai_dev_orchestrator.services.recovery_observer import RecoveryObserver as RealObserver
        store = SqliteExecutionStore(config.state.database_path)
        policy = RecoveryPolicy(
            config.github.repository_full_name,
            config.github.pull_request_base,
            config.execution.auto_merge,
            config.review.max_correction_attempts,
            config.github.status_for("completed"),
            config.execution.max_no_changes_attempts,
        )
        from ai_dev_orchestrator.services.escalation import EscalationService
        from ai_dev_orchestrator.adapters.github import GitHubProjectStatusAdapter
        return cls(store, RealObserver(config, store), RecoveryPlanner(policy), RecoveryExecutor(policy, store, RecoveryEffects(config)), config.providers.codex_model, config.providers.gemini_model, EscalationService(config, store, GitHubProjectStatusAdapter(config)))

    def resume(self, issue_number: int, *, retry_provider: bool = False, recover_failed: bool = False,
               resume_local_gates: bool = False,
               resume_publication: bool = False) -> ResumeResult:
        try:
            result = self._resume(issue_number, retry_provider=retry_provider, recover_failed=recover_failed,
                                  resume_local_gates=resume_local_gates,
                                  resume_publication=resume_publication)
        except Exception as error:
            run = self.store.get_active_for_issue(issue_number)
            if run and self.escalation:
                self.escalation.assess(run, error=error)
            raise
        if self.escalation:
            run = self.store.get(result.execution_id)
            if run.phase in PROVIDER_WAIT_PHASES or run.phase == ExecutionPhase.HUMAN_REQUIRED:
                run = self.escalation.assess(run)
                return self._result(run)
            if run.phase in {
                ExecutionPhase.FAILED,
                ExecutionPhase.NEEDS_CHANGES,
                ExecutionPhase.COMPLETED,
            }:
                self.escalation.deliver_event(run)
        return result

    def reconcile_external_merge(self, issue_number: int) -> ResumeResult:
        """Retoma HUMAN_REQUIRED somente após prova inequívoca de merge externo."""
        run = self.store.get_active_for_issue(issue_number)
        if run is None or run.phase is not ExecutionPhase.HUMAN_REQUIRED:
            raise ResumeError("Reconciliação externa exige execução HUMAN_REQUIRED ativa")
        self._validate_models(run)
        reconciled = self._reconcile_external_merge(run)
        if reconciled is None:
            return self._result(run)
        return self.resume(issue_number)

    def _resume(self, issue_number: int, *, retry_provider: bool = False, recover_failed: bool = False,
                resume_local_gates: bool = False,
                resume_publication: bool = False) -> ResumeResult:
        if issue_number <= 0:
            raise ResumeError("A Issue deve ser um inteiro positivo")
        run = self.store.get_active_for_issue(issue_number)
        if run is None:
            latest = self.store.get_latest_for_issue(issue_number)
            if latest is None:
                raise ResumeError(f"Nenhuma execução ativa para a Issue #{issue_number}")
            if recover_failed:
                from ai_dev_orchestrator.services.historical_recovery import recover_historical
                if ((self.codex_model is not None and latest.codex_model != self.codex_model)
                    or (self.gemini_model is not None and latest.gemini_model != self.gemini_model)):
                    raise ResumeError("Modelos configurados divergem do histórico")
                try:
                    run = recover_historical(self.store, latest, self.observer, self.planner)
                except Exception as error:
                    raise ResumeError(f"Recovery histórico bloqueado: {error}") from error
            else:
                raise ResumeError(f"A execução da Issue #{issue_number} já é terminal; use --recover-failed para reconciliar falha transitória")
        if run.phase in TERMINAL_PHASES:
            raise ResumeError(f"A execução da Issue #{issue_number} já é terminal")
        if run.phase is ExecutionPhase.HUMAN_REQUIRED:
            if resume_publication:
                self._validate_models(run)
                run = self._resume_publication(run)
            elif self._is_legacy_ci_terminal(run):
                run = self.store.transition(
                    run.id,
                    ExecutionPhase.WAITING_CI,
                    summary="Retomada de CI legada autorizada; identidade persistida será revalidada",
                )
            elif resume_local_gates and self._is_local_gate_resume_eligible(run):
                run = self.store.transition(
                    run.id,
                    ExecutionPhase.TESTING,
                    summary="Retomada humana autorizada; gates locais serão reexecutados na mesma execução",
                )
            elif not (retry_provider and run.provider_resume_phase):
                reconciled = self._reconcile_external_merge(run)
                if reconciled is None:
                    return self._result(run)
                run = reconciled
        self._validate_models(run)
        run = self.store.checkpoint(run.id, summary="Retomada iniciada")
        try:
            run = resume_provider_wait(self.store, run, manual_retry=retry_provider)
        except ProviderRecoveryError as error:
            raise ResumeError(str(error)) from error
        if run.phase in PROVIDER_WAIT_PHASES:
            return self._result(run)
        seen: set[tuple[object, ...]] = set()
        while True:
            try:
                observation = self.observer.observe(run)
            except KeyboardInterrupt:
                self.store.checkpoint(run.id, summary="Retomada interrompida")
                raise
            except ProviderFailure as error:
                run = self._record_provider_wait(run, error)
                return self._result(run)
            except Exception as error:
                raise ResumeError(f"Não foi possível observar a retomada: {error}") from error
            decision = self.planner.plan(run, observation)
            signature = (run.phase, run.branch, run.worktree_path, run.base_ref,
                         run.codex_session_id, run.pull_request_number, run.pull_request_url,
                         run.current_head_sha, run.ci_head_sha, run.reviewed_head_sha,
                         run.review_verdict, run.correction_attempts, run.merge_commit_sha,
                         run.merged_head_sha, run.project_status,
                         run.local_gate_correction_attempts, run.gate_results_json,
                         observation, decision.action, decision.next_phase)
            if signature in seen:
                raise ResumeError("Retomada sem progresso detectada")
            seen.add(signature)
            if decision.action.value == "BLOCK":
                from ai_dev_orchestrator.domain.recovery import CiState
                reason = "REMOTE_AMBIGUOUS"
                if (run.phase == ExecutionPhase.TESTING
                        and run.codex_start_attempted
                        and run.pull_request_number is None
                        and not observation.has_worktree_changes
                        and observation.local_head_sha == run.current_head_sha):
                    reason = "NO_CHANGES"
                elif run.review_verdict == "REJECTED" and run.correction_attempts >= self.planner.policy.max_correction_attempts:
                    reason = "CORRECTION_LIMIT"
                elif run.phase == ExecutionPhase.WAITING_CI and observation.ci.state == CiState.FAILURE:
                    reason = "CI_TERMINAL"
                elif run.phase in {ExecutionPhase.MERGING, ExecutionPhase.MERGE_PENDING}:
                    reason = "MERGE_BLOCKED"
                if self.escalation:
                    self.escalation.escalate(run, reason)
                else:
                    self.store.require_human(
                        run.id, summary="Reconciliação remota exige intervenção: " + decision.reason,
                        reason=reason,
                    )
                raise ResumeError(decision.reason)
            try:
                run = self.executor.execute(run, decision, observation)
            except KeyboardInterrupt:
                self.store.checkpoint(run.id, summary="Retomada interrompida")
                raise
            except ProviderFailure as error:
                run = self._record_provider_wait(run, error)
                return self._result(run)
            except Exception as error:
                raise ResumeError(f"Retomada interrompida em {run.phase}: {error}") from error
            if run.phase is ExecutionPhase.HUMAN_REQUIRED:
                return self._result(run)
            if run.phase in TERMINAL_PHASES:
                return self._result(run)
            if decision.action.value == "WAIT_FOR_CI" and run.phase.value == "WAITING_CI":
                return self._result(run)

    @staticmethod
    def _result(run: RunRecord) -> ResumeResult:
        return ResumeResult(
            run.issue_number,
            run.id,
            run.phase.value,
            run.branch,
            run.codex_session_id,
            run.pull_request_number,
            run.current_head_sha,
            run.correction_attempts,
            pull_request_url=run.pull_request_url,
            ci_status="SUCCESS" if run.ci_head_sha else None,
            review_verdict=run.review_verdict,
            merge_status="SUCCESS" if run.merge_commit_sha else "NOT_REQUESTED",
            project_status=run.project_status,
            quota_retry_at=run.quota_retry_at,
        )

    @staticmethod
    def _is_legacy_ci_terminal(run: RunRecord) -> bool:
        """Autoriza somente o bloqueio legado comprovadamente causado pela CI."""
        return (
            run.human_reason == "CI_TERMINAL"
            and run.human_phase == ExecutionPhase.WAITING_CI.value
            and bool(
                run.branch
                and run.worktree_path
                and run.base_ref
                and run.codex_session_id
                and run.pull_request_number
                and run.pull_request_url
                and run.current_head_sha
            )
        )

    @staticmethod
    def _is_local_gate_correction_limit(run: RunRecord) -> bool:
        """Autoriza somente a repetição humana dos gates locais já bloqueados."""
        return (
            run.human_reason == "LOCAL_GATE_CORRECTION_LIMIT"
            and run.human_phase == ExecutionPhase.TESTING.value
            and bool(run.branch and run.worktree_path and run.base_ref and run.codex_session_id)
        )

    @classmethod
    def _is_local_gate_resume_eligible(cls, run: RunRecord) -> bool:
        """Aceita o limite explícito ou o legado comprovadamente interrompido pelo anti-loop."""
        if cls._is_local_gate_correction_limit(run):
            return True
        return (
            run.human_reason == "INTERNAL_ERROR"
            and run.human_phase == ExecutionPhase.TESTING.value
            and run.pull_request_number is None
            and run.pull_request_url is None
            and bool(run.branch and run.worktree_path and run.base_ref and run.codex_session_id)
            and (run.local_gate_correction_attempts > 0 or run.gate_results_json is not None)
        )

    def _resume_publication(self, run: RunRecord) -> RunRecord:
        """Restaura uma fase de publicação somente após prova read-only do planner."""
        allowed_reasons = {"INTERNAL_ERROR", "REMOTE_AMBIGUOUS"}
        allowed_phases = {
            ExecutionPhase.COMMIT_PENDING,
            ExecutionPhase.PUSH_PENDING,
            ExecutionPhase.PR_PENDING,
            ExecutionPhase.PUBLISHING,
        }
        if run.human_reason not in allowed_reasons:
            raise ResumeError(
                "Recuperação de publicação não autorizada para o motivo HUMAN_REQUIRED atual"
            )
        try:
            target = ExecutionPhase(run.human_phase or "")
        except ValueError as error:
            raise ResumeError(
                "Recuperação de publicação exige human_phase reconhecida"
            ) from error
        if target not in allowed_phases:
            raise ResumeError(
                "Recuperação de publicação não autorizada para a human_phase atual"
            )

        candidate = replace(run, phase=target)
        try:
            observation = self.observer.observe(candidate)
        except Exception as error:
            raise ResumeError(
                f"Não foi possível observar a recuperação de publicação: {error}"
            ) from error
        decision = self.planner.plan(candidate, observation)
        if decision.action is RecoveryAction.BLOCK:
            raise ResumeError(
                "Recuperação de publicação bloqueada: " + decision.reason
            )
        return self.store.transition(
            run.id,
            target,
            summary=(
                "Retomada explícita de publicação autorizada após observação segura: "
                f"{decision.action.value}"
            ),
        )

    def _validate_models(self, run: RunRecord) -> None:
        if (
            (self.codex_model is not None and run.codex_model != self.codex_model)
            or (self.gemini_model is not None and run.gemini_model != self.gemini_model)
        ):
            raise ResumeError(
                "Os modelos configurados divergem dos modelos persistidos nesta execução"
            )

    def _reconcile_external_merge(self, run: RunRecord) -> RunRecord | None:
        """Converge HUMAN_REQUIRED quando o merge remoto final é inequívoco."""
        if (
            not run.branch
            or not run.current_head_sha
            or not run.pull_request_number
            or not run.pull_request_url
        ):
            return None
        try:
            observation = self.observer.observe(run)
        except Exception:
            return None
        if len(observation.pull_requests) != 1:
            return None
        pull_request = observation.pull_requests[0]
        merge = observation.merge
        if (
            pull_request.number != run.pull_request_number
            or pull_request.url != run.pull_request_url
            or pull_request.repository_full_name != self.planner.policy.repository_full_name
            or pull_request.base != self.planner.policy.pull_request_base
            or pull_request.head_branch != run.branch
            or pull_request.head_sha != run.current_head_sha
            or pull_request.state is not PullRequestState.MERGED
            or merge.state is not MergeState.MERGED
            or merge.merged_head_sha != run.current_head_sha
            or not merge.merge_commit_sha
        ):
            return None
        return self.store.transition(
            run.id,
            ExecutionPhase.PROJECT_DONE_PENDING,
            summary="Merge externo reconciliado por identidade remota completa",
            merged_head_sha=merge.merged_head_sha,
            merge_commit_sha=merge.merge_commit_sha,
            merge_origin="EXTERNAL",
        )

    def _record_provider_wait(self, run: RunRecord, failure: ProviderFailure) -> RunRecord:
        current = record_provider_failure(self.store, run.id, failure)
        if current.phase == ExecutionPhase.BLOCKED_PROVIDER:
            raise ResumeError(current.last_error)
        return current
