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


class ResumeService:
    def __init__(self, store: SqliteExecutionStore, observer: RecoveryObserver,
                 planner: RecoveryPlanner, executor: RecoveryExecutor) -> None:
        self.store, self.observer, self.planner, self.executor = store, observer, planner, executor

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "ResumeService":
        from ai_dev_orchestrator.domain.recovery import RecoveryPolicy
        from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
        from ai_dev_orchestrator.services.recovery_observer import RecoveryObserver as RealObserver
        store = SqliteExecutionStore(config.state.database_path)
        policy = RecoveryPolicy(config.github.repository_full_name, config.github.pull_request_base, config.execution.auto_merge, config.review.max_correction_attempts)
        return cls(store, RealObserver(config, store), RecoveryPlanner(policy), RecoveryExecutor(policy, store, RecoveryEffects(config)))

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
        run = self.store.checkpoint(run.id, summary="Retomada iniciada")
        seen: set[tuple[object, ...]] = set()
        while True:
            observation = self.observer.observe(run)
            decision = self.planner.plan(run, observation)
            signature = (run.phase, run.current_head_sha, run.codex_session_id,
                         run.pull_request_number, run.reviewed_head_sha,
                         observation, decision.action, decision.next_phase)
            if signature in seen:
                raise ResumeError("Retomada sem progresso detectada")
            seen.add(signature)
            if decision.action.value == "BLOCK":
                raise ResumeError(decision.reason)
            try:
                run = self.executor.execute(run, decision, observation)
            except Exception as error:
                raise ResumeError(f"Retomada interrompida em {run.phase}: {error}") from error
            if run.phase in TERMINAL_PHASES:
                return ResumeResult(run.issue_number, run.id, run.phase.value, run.branch,
                                    run.codex_session_id, run.pull_request_number,
                                    run.current_head_sha, run.correction_attempts)
