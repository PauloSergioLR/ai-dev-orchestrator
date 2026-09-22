"""Heartbeat compacto para processos longos sem revelar sua saída."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from math import isfinite
from threading import Event, Thread
from time import monotonic
from typing import Iterator


@contextmanager
def heartbeat(
    label: str,
    progress: Callable[[str], None] | None,
    interval_seconds: float = 300,
    detail: Callable[[], str] | None = None,
) -> Iterator[None]:
    """Emite início, pulsos espaçados e fim; o trabalho permanece na thread chamadora."""
    if not isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("Intervalo do heartbeat deve ser positivo e finito")
    if progress is None:
        yield
        return
    progress(f"{label} iniciado")
    stopped = Event()
    started = monotonic()

    def pulse() -> None:
        while not stopped.wait(interval_seconds):
            elapsed = max(1, int((monotonic() - started) // 60))
            suffix = f"; {detail()}" if detail is not None else ""
            progress(f"{label} em execução há {elapsed} min{suffix}")

    thread = Thread(target=pulse, name="orchestrator-heartbeat", daemon=True)
    thread.start()
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        stopped.set()
        thread.join(timeout=1)
        progress(f"{label} concluído" if succeeded else f"{label} interrompido")
