"""Supersessão explícita e auditável de execuções que não podem ser retomadas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import MergeState, PullRequestState, RecoveryObservation
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery_observer import RecoveryObservationError, RecoveryObserver


class SupersessionError(Exception):
    """A prova remota não permite abandonar a execução com segurança."""


class ExecutionObserver(Protocol):
    def observe(self, run: RunRecord) -> RecoveryObservation: ...


@dataclass(frozen=True)
class SupersessionPreview:
    run: RunRecord
    remote_head_sha: str | None
    pull_request_state: PullRequestState

    @property
    def evidence(self) -> str:
        return (
            f"execução {self.run.id}; fase {self.run.phase}; PR #{self.run.pull_request_number}; "
            f"branch {self.run.branch}; HEAD persistido {self.run.current_head_sha or '-'}; "
            f"HEAD remoto {self.remote_head_sha or '-'}; PR {self.pull_request_state}"
        )


class SupersessionService:
    """Só permite supersessão após observar o PR esperado, fechado e sem merge."""

    def __init__(self, store: SqliteExecutionStore, observer: ExecutionObserver) -> None:
        self.store = store
        self.observer = observer

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "SupersessionService":
        store = SqliteExecutionStore(config.state.database_path)
        return cls(store, RecoveryObserver(config, store))

    def preview(self, issue_number: int) -> SupersessionPreview:
        run = self.store.get_latest_for_issue(issue_number)
        if run is None:
            raise SupersessionError(f"Nenhuma execução encontrada para a Issue #{issue_number}")
        if run.phase in {ExecutionPhase.COMPLETED, ExecutionPhase.SUPERSEDED}:
            raise SupersessionError("A execução já é terminal e não pode ser supersedida")
        if run.pull_request_number is None or not run.pull_request_url or not run.branch:
            raise SupersessionError("Execução sem identidade completa de Pull Request não pode ser supersedida")
        other = self.store.get_active_for_issue(issue_number)
        if other is not None and other.id != run.id:
            raise SupersessionError("Outra execução ativa para a mesma Issue impede supersessão")
        try:
            observation = self.observer.observe(run)
        except RecoveryObservationError as error:
            raise SupersessionError("Estado remoto desconhecido; supersessão bloqueada") from error
        if len(observation.pull_requests) != 1:
            raise SupersessionError("Pull Requests remotos ambíguos; supersessão bloqueada")
        pull_request = observation.pull_requests[0]
        if (
            pull_request.number != run.pull_request_number
            or pull_request.url != run.pull_request_url
            or pull_request.head_branch != run.branch
        ):
            raise SupersessionError("Pull Request remoto não corresponde à identidade persistida")
        if pull_request.state is PullRequestState.MERGED or observation.merge.state is MergeState.MERGED:
            raise SupersessionError("Pull Request foi mergeado; use reconciliação de merge, não supersessão")
        if pull_request.state is PullRequestState.OPEN:
            raise SupersessionError("Pull Request continua aberto; a recuperação normal é obrigatória")
        if pull_request.state is not PullRequestState.CLOSED or observation.merge.state is not MergeState.CLOSED:
            raise SupersessionError("Estado remoto do Pull Request não permite supersessão")
        return SupersessionPreview(run, observation.remote_head_sha, pull_request.state)

    def supersede(self, issue_number: int, reason: str) -> RunRecord:
        preview = self.preview(issue_number)
        text = reason.strip()
        if not text:
            raise SupersessionError("O motivo da supersessão é obrigatório")
        return self.store.supersede(
            preview.run.id,
            summary=("Execução supersedida por decisão humana; motivo: " + text + "; " + preview.evidence),
        )
