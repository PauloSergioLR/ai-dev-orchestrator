"""Heartbeat compacto para processos longos sem revelar sua saída."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic
from typing import Iterator


@contextmanager
def heartbeat(
    label: str,
    progress: Callable[[str], None] | None,
    interval_seconds: float = 300,
) -> Iterator[None]:
    """Emite início, pulsos espaçados e fim; o trabalho permanece na thread chamadora."""
    if progress is None:
        yield
        return
    progress(f"{label} iniciado")
    stopped = Event()
    started = monotonic()

    def pulse() -> None:
        while not stopped.wait(interval_seconds):
            elapsed = max(1, int((monotonic() - started) // 60))
            progress(f"{label} em execução há {elapsed} min")

    thread = Thread(target=pulse, name="orchestrator-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        progress(f"{label} concluído")
