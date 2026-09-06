"""Leitura de histórico e métricas derivadas exclusivamente do journal local."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ai_dev_orchestrator.domain.execution import ExecutionEvent, ExecutionPhase, RunRecord
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


@dataclass(frozen=True)
class ExecutionMetrics:
    run: RunRecord
    duration: timedelta
    ci_wait: timedelta
    quota_wait: timedelta
    reviews: int


class HistoryService:
    def __init__(self, store: SqliteExecutionStore) -> None:
        self.store = store

    def list(self, issue_number: int | None = None) -> tuple[ExecutionMetrics, ...]:
        return tuple(self.metrics(run) for run in self.store.list_history(issue_number))

    def metrics(self, run: RunRecord) -> ExecutionMetrics:
        events = self.store.events(run.id)
        ci_wait = _time_in(events, {ExecutionPhase.WAITING_CI}, run.updated_at)
        quota_wait = _time_in(
            events,
            {ExecutionPhase.WAITING_CODEX_QUOTA, ExecutionPhase.WAITING_GEMINI_QUOTA},
            run.updated_at,
        )
        reviews = sum(event.summary == "Review independente persistida" for event in events)
        return ExecutionMetrics(run, run.updated_at - run.created_at, ci_wait, quota_wait, reviews)


def _time_in(events: tuple[ExecutionEvent, ...], phases: set[ExecutionPhase], end) -> timedelta:
    total = timedelta()
    for index, event in enumerate(events):
        if event.phase in phases:
            next_time = events[index + 1].created_at if index + 1 < len(events) else end
            total += max(next_time - event.created_at, timedelta())
    return total


def format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s" if hours else f"{minutes}m{seconds:02d}s"
