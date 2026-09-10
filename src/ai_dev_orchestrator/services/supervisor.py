"""Supervisor com concorrência lógica controlada e checkpoints duráveis."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Callable, Iterator

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, PROVIDER_WAIT_PHASES
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.work import WorkResult, WorkService
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.services.cleanup import CleanupService
from ai_dev_orchestrator.services.history import HistoryService, format_duration
from ai_dev_orchestrator.services.escalation import EscalationService


class SupervisorError(Exception):
    pass


class SupervisorService:
    def __init__(
        self,
        config: OrchestratorConfig,
        work_service: WorkService,
        store: SqliteExecutionStore,
        sleep: Callable[[float], None] = time.sleep,
        escalation: EscalationService | None = None,
    ) -> None:
        self.config, self.work_service, self.store, self.sleep = (
            config,
            work_service,
            store,
            sleep,
        )
        self.escalation = escalation or EscalationService(config, store)
        self._assessed_runs: set[str] = set()

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "SupervisorService":
        from ai_dev_orchestrator.adapters.github import GitHubProjectStatusAdapter
        store = SqliteExecutionStore(config.state.database_path)
        return cls(
            config,
            WorkService.from_config(config),
            store,
            escalation=EscalationService(config, store, GitHubProjectStatusAdapter(config)),
        )

    def watch(self) -> None:
        lock = self.config.state.database_path.with_suffix(".watch.lock")
        with _exclusive_lock(lock):
            if self.config.execution.max_parallel_runs == 1:
                self._watch_sequential()
                return
            self._watch_parallel()

    def _watch_sequential(self) -> None:
        """Implementação legada, preservada literalmente para o default seguro."""
        while True:
            active = self.store.list_active()
            if len(active) > 1:
                raise SupervisorError("Mais de uma execução ativa foi encontrada")
            if active and (active[0].phase in PROVIDER_WAIT_PHASES or active[0].phase == ExecutionPhase.HUMAN_REQUIRED):
                run = self.escalation.assess(active[0])
                if run.phase == ExecutionPhase.HUMAN_REQUIRED:
                    raise SupervisorError("HUMAN_REQUIRED: intervenção humana necessária; consulte orch history")
            if active and active[0].phase in PROVIDER_WAIT_PHASES:
                run = active[0]
                if run.phase == ExecutionPhase.BLOCKED_PROVIDER:
                    raise SupervisorError(f"Provider exige intervenção: {run.last_error}")
                retry_at = run.quota_retry_at
                policy_retry = False
                if retry_at is None:
                    if run.phase == ExecutionPhase.WAITING_PROVIDER:
                        raise SupervisorError("Espera transitória sem retry comprovado")
                    interval = self.config.supervisor.retry_without_reset_seconds
                    if interval is None:
                        raise SupervisorError(
                            "Provider não informou retry e nenhuma política segura foi configurada"
                        )
                    retry_at = run.quota_observed_at
                    if retry_at is None:
                        raise SupervisorError("Checkpoint de quota incompleto")
                    retry_at = datetime.fromtimestamp(
                        retry_at.timestamp() + interval, timezone.utc
                    )
                    policy_retry = True
                remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    self.sleep(min(remaining, self.config.supervisor.max_sleep_seconds))
                    continue
                if policy_retry:
                    self.store.checkpoint(run.id, summary="Intervalo da política local foi alcançado", quota_retry_at=retry_at.isoformat())
            try:
                result = self.work_service.work()
            except RunPipelineError as error:
                active_after_error = self.store.list_active()
                if len(active_after_error) == 1 and active_after_error[0].phase in PROVIDER_WAIT_PHASES:
                    continue
                if getattr(error, "reason", None) == "CI_FAILURE_RECOVERY":
                    continue
                raise
            if result is None:
                return
            if _is_waiting(result):
                self.sleep(self.config.supervisor.poll_interval_seconds)
                continue
            self._show_completion(result)

    def _watch_parallel(self) -> None:
        """Avança cada run isoladamente e ocupa vagas em ordem determinística.

        Uma espera de provider permanece no banco e conta como slot lógico, mas
        nunca impede que os outros slots sejam reconciliados ou preenchidos.
        """
        limit = self.config.execution.max_parallel_runs
        while True:
            active = self.store.list_active()
            if len([run for run in active if self._occupies_slot(run)]) > limit:
                raise SupervisorError("Execuções ativas excedem max_parallel_runs; estado não é seguro")

            progressed = False
            for run in active:
                if run.phase == ExecutionPhase.HUMAN_REQUIRED:
                    self._assess_once(run)
                    continue
                if run.phase in PROVIDER_WAIT_PHASES:
                    if run.phase == ExecutionPhase.BLOCKED_PROVIDER:
                        self._assess_once(run)
                        continue
                    if not self._provider_ready(run):
                        continue
                try:
                    result = self.work_service.resume_issue(run.issue_number)
                except RunPipelineError as error:
                    current = self.store.get_active_for_issue(run.issue_number)
                    if current and (current.phase in PROVIDER_WAIT_PHASES or current.phase == ExecutionPhase.HUMAN_REQUIRED):
                        continue
                    if getattr(error, "reason", None) == "CI_FAILURE_RECOVERY":
                        continue
                    raise
                progressed = not _is_waiting(result)
                self._show_completion(result)

            active = self.store.list_active()
            while len([run for run in active if self._occupies_slot(run)]) < limit:
                try:
                    result = self.work_service.start_next(
                        frozenset(run.issue_number for run in active)
                    )
                except RunPipelineError as error:
                    active_after_error = self.store.list_active()
                    new_run = next(
                        (run for run in active_after_error if run.issue_number not in {item.issue_number for item in active}),
                        None,
                    )
                    if new_run and (
                        new_run.phase in PROVIDER_WAIT_PHASES
                        or new_run.phase == ExecutionPhase.HUMAN_REQUIRED
                    ):
                        active = active_after_error
                        continue
                    if getattr(error, "reason", None) == "CI_FAILURE_RECOVERY":
                        active = active_after_error
                        continue
                    raise
                if result is None:
                    break
                progressed = progressed or not _is_waiting(result)
                self._show_completion(result)
                active = self.store.list_active()

            active = self.store.list_active()
            if not [run for run in active if self._occupies_slot(run)] and not progressed:
                return
            if not progressed:
                self.sleep(self.config.supervisor.poll_interval_seconds)

    def _provider_ready(self, run) -> bool:
        if run.phase == ExecutionPhase.BLOCKED_PROVIDER:
            return False
        now = datetime.now(timezone.utc)
        retry_at = run.quota_retry_at
        if retry_at is None:
            if run.phase == ExecutionPhase.WAITING_PROVIDER:
                return False
            interval = self.config.supervisor.retry_without_reset_seconds
            if interval is None or run.quota_observed_at is None:
                return False
            retry_at = datetime.fromtimestamp(run.quota_observed_at.timestamp() + interval, timezone.utc)
            if retry_at > now:
                return False
            self.store.checkpoint(run.id, summary="Intervalo da política local foi alcançado", quota_retry_at=retry_at.isoformat())
        return retry_at <= now

    def _assess_once(self, run) -> None:
        """Entrega o escalonamento uma vez por processo, sem polling agressivo."""
        if run.id not in self._assessed_runs:
            self.escalation.assess(run)
            self._assessed_runs.add(run.id)

    @staticmethod
    def _occupies_slot(run) -> bool:
        """Bloqueios humanos não consomem capacidade de execução autônoma."""
        return run.phase not in {
            ExecutionPhase.HUMAN_REQUIRED,
            ExecutionPhase.BLOCKED_PROVIDER,
        }

    def _show_completion(self, result: WorkResult) -> None:
        issue = result.run.issue_number if result.run else (result.resume.issue_number if result.resume else None)
        if issue is None:
            return
        run = self.store.get_latest_for_issue(issue)
        if run is None or run.phase is not ExecutionPhase.COMPLETED:
            return
        metrics = HistoryService(self.store).metrics(run)
        correction = "correção" if run.correction_attempts == 1 else "correções"
        pr = f"PR #{run.pull_request_number}" if run.pull_request_number else "sem PR"
        print(f"#{run.issue_number} COMPLETED | {pr} | {run.correction_attempts} {correction} | {metrics.reviews} reviews | {format_duration(metrics.duration)}")
        if self.config.cleanup.auto_cleanup:
            CleanupService(self.config, self.store, GitWorktreeAdapter()).cleanup(run.id)


def _is_waiting(result: WorkResult) -> bool:
    return bool(
        result.resumed
        and result.resume
        and result.resume.phase
        in {"WAITING_CODEX_QUOTA", "WAITING_GEMINI_QUOTA", "WAITING_PROVIDER", "WAITING_CI"}
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise SupervisorError("Já existe uma instância de orch watch ativa") from error
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)
