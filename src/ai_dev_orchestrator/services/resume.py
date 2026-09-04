"""Orquestra uma retomada segura sem escolher ações fora do planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ai_dev_orchestrator.domain.execution import RunRecord, TERMINAL_PHASES
from ai_dev_orchestrator.domain.recovery import RecoveryObservation
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_executor import RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
from datetime import datetime, timezone


class ResumeError(Exception):
    """A retomada não pode continuar com segurança."""


class RecoveryObserver(Protocol):
    def observe(self, run: RunRecord) -> RecoveryObservation: ...


class HumanEscalator(Protocol):
    def escalate(
        self, execution_id: str, reason_code: str, reason: str, **details: object
    ) -> RunRecord: ...


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
                 codex_model: str | None = None, gemini_model: str | None = None,
                 escalator: HumanEscalator | None = None,
                 ci_timeout_seconds: float | None = None) -> None:
        self.store, self.observer, self.planner, self.executor = store, observer, planner, executor
        self.codex_model, self.gemini_model = codex_model, gemini_model
        self.escalator = escalator
        self.ci_timeout_seconds = ci_timeout_seconds

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "ResumeService":
        from ai_dev_orchestrator.domain.recovery import RecoveryPolicy
        from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
        from ai_dev_orchestrator.services.recovery_observer import RecoveryObserver as RealObserver
        store = SqliteExecutionStore(config.state.database_path)
        policy = RecoveryPolicy(config.github.repository_full_name, config.github.pull_request_base, config.execution.auto_merge, config.review.max_correction_attempts, config.github.done_status)
        from ai_dev_orchestrator.services.escalation import HumanEscalationService
        return cls(store, RealObserver(config, store), RecoveryPlanner(policy), RecoveryExecutor(policy, store, RecoveryEffects(config)), config.providers.codex_model, config.providers.gemini_model, HumanEscalationService.from_config(config), config.ci.timeout_seconds)

    def resume(self, issue_number: int) -> ResumeResult:
        if issue_number <= 0:
            raise ResumeError("A Issue deve ser um inteiro positivo")
        run = self.store.get_active_for_issue(issue_number)
        if run is None:
            latest = self.store.get_latest_for_issue(issue_number)
            if latest is None:
                raise ResumeError(f"Nenhuma execução ativa para a Issue #{issue_number}")
            raise ResumeError(f"A execução da Issue #{issue_number} já é terminal")
        if run.phase in TERMINAL_PHASES:
            raise ResumeError(f"A execução da Issue #{issue_number} já é terminal")
        if (
            (self.codex_model is not None and run.codex_model != self.codex_model)
            or (self.gemini_model is not None and run.gemini_model != self.gemini_model)
        ):
            raise ResumeError(
                "Os modelos configurados divergem dos modelos persistidos nesta execução"
            )
        run = self.store.checkpoint(run.id, summary="Retomada iniciada")
        run = self._resume_quota_if_due(run)
        if run.phase in TERMINAL_PHASES:
            return self._result(run)
        if run.phase in {
            ExecutionPhase.WAITING_CODEX_QUOTA,
            ExecutionPhase.WAITING_GEMINI_QUOTA,
        }:
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
                if self.escalator:
                    run = self.escalator.escalate(
                        run.id,
                        "ORCHESTRATOR_INTERNAL_ERROR",
                        f"Não foi possível observar a retomada: {error}",
                        suggested_action="Inspecione o estado local e remoto antes de retomar.",
                    )
                    return self._result(run)
                raise ResumeError(f"Não foi possível observar a retomada: {error}") from error
            decision = self.planner.plan(run, observation)
            if (
                self.escalator
                and run.phase == ExecutionPhase.WAITING_CI
                and decision.action.value == "WAIT_FOR_CI"
                and self._ci_wait_expired(run)
            ):
                run = self.escalator.escalate(
                    run.id,
                    "CI_STUCK_TIMEOUT",
                    "CI permaneceu pendente além do timeout configurado.",
                    suggested_action="Inspecione os checks pendentes e retome a mesma execução.",
                )
                return self._result(run)
            signature = (run.phase, run.branch, run.worktree_path, run.base_ref,
                         run.codex_session_id, run.pull_request_number, run.pull_request_url,
                         run.current_head_sha, run.ci_head_sha, run.reviewed_head_sha,
                         run.review_verdict, run.correction_attempts, run.merge_commit_sha,
                         run.merged_head_sha, run.project_status,
                         observation, decision.action, decision.next_phase)
            if signature in seen:
                raise ResumeError("Retomada sem progresso detectada")
            seen.add(signature)
            if decision.action.value == "BLOCK":
                if self.escalator:
                    run = self.escalator.escalate(
                        run.id,
                        _recovery_reason_code(decision.reason),
                        decision.reason,
                        suggested_action="Reconcilie o estado indicado e retome a mesma execução.",
                    )
                    return self._result(run)
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
                if self.escalator:
                    run = self.escalator.escalate(
                        run.id,
                        _recovery_reason_code(str(error)),
                        str(error),
                        suggested_action="Corrija a causa e retome a mesma execução.",
                    )
                    return self._result(run)
                raise ResumeError(f"Retomada interrompida em {run.phase}: {error}") from error
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

    def _resume_quota_if_due(self, run: RunRecord) -> RunRecord:
        if run.phase not in {
            ExecutionPhase.WAITING_CODEX_QUOTA,
            ExecutionPhase.WAITING_GEMINI_QUOTA,
        }:
            return run
        retry_at = run.quota_retry_at
        if retry_at is None:
            if self.escalator:
                return self.escalator.escalate(
                    run.id,
                    "QUOTA_WITHOUT_SAFE_RETRY",
                    "Provider não informou um instante confiável para retry.",
                    classification=run.quota_classification,
                    suggested_action="Verifique a quota do provider e retome manualmente quando estiver disponível.",
                )
            raise ResumeError(
                "Provider não informou quando retentar; intervenção necessária"
            )
        if retry_at > datetime.now(timezone.utc):
            return run
        target = (
            ExecutionPhase.CODEX_RUNNING
            if run.phase == ExecutionPhase.WAITING_CODEX_QUOTA
            else ExecutionPhase.GEMINI_REVIEWING
        )
        return self.store.transition(
            run.id,
            target,
            summary="Janela de retry informada pelo provider foi alcançada",
            quota_provider=None,
            quota_classification=None,
            quota_observed_at=None,
            quota_retry_at=None,
            last_error=None,
        )

    def _record_provider_wait(
        self, run: RunRecord, failure: ProviderFailure
    ) -> RunRecord:
        if failure.classification not in {
            ProviderFailureKind.TRANSIENT_RATE_LIMIT,
            ProviderFailureKind.TERMINAL_QUOTA,
        }:
            if self.escalator:
                return self.escalator.escalate(
                    run.id,
                    failure.classification.value,
                    str(failure),
                    classification=failure.classification.value,
                    suggested_action="Corrija a autenticação/modelo do provider e retome a mesma execução.",
                )
            self.store.transition(
                run.id,
                ExecutionPhase.FAILED,
                summary="Falha terminal do provider",
                quota_provider=failure.provider,
                quota_classification=failure.classification.value,
                quota_observed_at=failure.observed_at.isoformat(),
                last_error=str(failure),
            )
            raise ResumeError(str(failure))
        phase = (
            ExecutionPhase.WAITING_CODEX_QUOTA
            if failure.provider == "codex"
            else ExecutionPhase.WAITING_GEMINI_QUOTA
        )
        return self.store.transition(
            run.id,
            phase,
            summary="Provider indisponível por limite de uso",
            quota_provider=failure.provider,
            quota_classification=failure.classification.value,
            quota_observed_at=failure.observed_at.isoformat(),
            quota_retry_at=failure.retry_at.isoformat() if failure.retry_at else None,
            codex_session_id=failure.session_id or run.codex_session_id,
            last_error=str(failure),
        )

    def _ci_wait_expired(self, run: RunRecord) -> bool:
        if self.ci_timeout_seconds is None:
            return False
        entered = next(
            (
                event.created_at
                for event in self.store.events(run.id)
                if event.phase == ExecutionPhase.WAITING_CI
                and event.previous_phase != ExecutionPhase.WAITING_CI
            ),
            run.updated_at,
        )
        return (datetime.now(timezone.utc) - entered).total_seconds() >= self.ci_timeout_seconds


def _recovery_reason_code(reason: str) -> str:
    normalized = reason.casefold()
    if "limite" in normalized and "corre" in normalized:
        return "CORRECTION_LIMIT_REACHED"
    if "ci " in normalized and "falh" in normalized:
        return "CI_TERMINAL_FAILURE"
    if "merge" in normalized:
        return "MERGE_IMPOSSIBLE"
    if "amb" in normalized or "desconhecido" in normalized:
        return "REMOTE_STATE_AMBIGUOUS"
    if "diverg" in normalized or "converg" in normalized or "head" in normalized:
        return "GIT_PR_HEAD_DIVERGENCE"
    return "RECOVERY_HUMAN_REQUIRED"
