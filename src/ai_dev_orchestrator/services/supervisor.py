"""Supervisor com concorrência opt-in e checkpoints duráveis por execução."""

from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Callable, Iterator

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.work import WorkResult, WorkService
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.services.cleanup import CleanupService
from ai_dev_orchestrator.services.history import HistoryService, format_duration


class SupervisorError(Exception):
    pass


class SupervisorService:
    def __init__(
        self,
        config: OrchestratorConfig,
        work_service: WorkService,
        store: SqliteExecutionStore,
        sleep: Callable[[float], None] = time.sleep,
        work_service_factory: Callable[[], WorkService] | None = None,
    ) -> None:
        self.config, self.work_service, self.store, self.sleep = (
            config,
            work_service,
            store,
            sleep,
        )
        self.work_service_factory = work_service_factory or (
            lambda: WorkService.from_config(config)
        )

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "SupervisorService":
        return cls(
            config,
            WorkService.from_config(config),
            SqliteExecutionStore(config.state.database_path),
        )

    def watch(self) -> None:
        lock = self.config.state.database_path.with_suffix(".watch.lock")
        with _exclusive_lock(lock):
            if self.config.execution.max_parallel_runs == 1:
                self._watch_sequential()
                return
            self._watch_parallel()

    def _watch_sequential(self) -> None:
        """Preserva integralmente o comportamento conservador já existente."""
        while True:
            active = self.store.list_active()
            if len(active) > 1:
                raise SupervisorError("Mais de uma execução ativa foi encontrada")
            if active and active[0].phase in {
                ExecutionPhase.WAITING_CODEX_QUOTA,
                ExecutionPhase.WAITING_GEMINI_QUOTA,
            }:
                run = active[0]
                retry_at = run.quota_retry_at
                policy_retry = False
                if retry_at is None:
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
                    self.sleep(
                        min(remaining, self.config.supervisor.max_sleep_seconds)
                    )
                    continue
                if policy_retry:
                    target = (
                        ExecutionPhase.CODEX_RUNNING
                        if run.phase == ExecutionPhase.WAITING_CODEX_QUOTA
                        else ExecutionPhase.GEMINI_REVIEWING
                    )
                    self.store.transition(
                        run.id,
                        target,
                        summary="Intervalo seguro da política local foi alcançado",
                        quota_provider=None,
                        quota_classification=None,
                        quota_observed_at=None,
                        quota_retry_at=None,
                        last_error=None,
                    )
            try:
                result = self.work_service.work()
            except RunPipelineError:
                # O pipeline sinaliza a quota depois de persistir o checkpoint.
                # Só a evidência inequívoca no store autoriza o supervisor a
                # converter esse erro em espera; demais falhas continuam terminais.
                active_after_error = self.store.list_active()
                if len(active_after_error) == 1 and active_after_error[0].phase in {
                    ExecutionPhase.WAITING_CODEX_QUOTA,
                    ExecutionPhase.WAITING_GEMINI_QUOTA,
                }:
                    continue
                raise
            if result is None:
                return
            if _is_waiting(result):
                self.sleep(self.config.supervisor.poll_interval_seconds)
                continue
            self._show_completion(result)
            # Uma conclusão libera a seleção da próxima Issue Ready.

    def _watch_parallel(self) -> None:
        """Agenda cada Issue uma vez por ciclo, até o limite configurado."""
        futures: dict[int, Future[WorkResult | None]] = {}
        limit = self.config.execution.max_parallel_runs
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="orch-run") as pool:
            while True:
                for issue, future in tuple(futures.items()):
                    if not future.done():
                        continue
                    del futures[issue]
                    try:
                        result = future.result()
                    except RunPipelineError:
                        # Quota já foi checkpointada pelo pipeline; as demais
                        # Issues não devem ficar paradas por essa espera.
                        continue
                    except Exception as error:
                        print(f"#{issue} interrompida: {error}")
                        continue
                    if result is not None:
                        self._show_completion(result)

                active = self.store.list_active()
                active_by_issue = {run.issue_number: run for run in active}
                for issue, run in active_by_issue.items():
                    if issue in futures or self._is_quota_not_due(run):
                        continue
                    if len(futures) >= limit:
                        break
                    futures[issue] = pool.submit(self._work_issue, issue)

                occupied = set(active_by_issue) | set(futures)
                vacancies = limit - max(len(active_by_issue), len(futures))
                if vacancies > 0:
                    for issue in self._eligible_issue_numbers(occupied)[:vacancies]:
                        futures[issue] = pool.submit(self._work_issue, issue)

                if not active_by_issue and not futures:
                    return
                self.sleep(self.config.supervisor.poll_interval_seconds)

    def _work_issue(self, issue: int) -> WorkResult | None:
        # WorkService/RunPipeline mantêm estado transitório da execução. Uma
        # instância por tarefa impede compartilhar execution_id ou sessão.
        service = self.work_service_factory()
        return service.work_issue(issue)

    def _eligible_issue_numbers(self, excluded: set[int]) -> tuple[int, ...]:
        service = self.work_service_factory()
        return service.eligible_issue_numbers(excluded)

    def _is_quota_not_due(self, run: object) -> bool:
        if getattr(run, "phase", None) not in {
            ExecutionPhase.WAITING_CODEX_QUOTA,
            ExecutionPhase.WAITING_GEMINI_QUOTA,
        }:
            return False
        retry_at = getattr(run, "quota_retry_at", None)
        if retry_at is None:
            interval = self.config.supervisor.retry_without_reset_seconds
            observed_at = getattr(run, "quota_observed_at", None)
            if interval is None or observed_at is None:
                return True
            retry_at = datetime.fromtimestamp(
                observed_at.timestamp() + interval, timezone.utc
            )
            if retry_at <= datetime.now(timezone.utc):
                target = (
                    ExecutionPhase.CODEX_RUNNING
                    if run.phase == ExecutionPhase.WAITING_CODEX_QUOTA
                    else ExecutionPhase.GEMINI_REVIEWING
                )
                self.store.transition(
                    run.id,
                    target,
                    summary="Intervalo seguro da política local foi alcançado",
                    quota_provider=None,
                    quota_classification=None,
                    quota_observed_at=None,
                    quota_retry_at=None,
                    last_error=None,
                )
                return False
        return retry_at > datetime.now(timezone.utc)

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
        in {"WAITING_CODEX_QUOTA", "WAITING_GEMINI_QUOTA", "WAITING_CI"}
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
