"""Polling pequeno e reutilizável para consistência eventual do GitHub."""

from __future__ import annotations

from enum import Enum
import time
from typing import Callable, TypeVar

from ai_dev_orchestrator.config import ConvergenceConfig


T = TypeVar("T")


class ConvergenceError(Exception):
    """Falha controlada ao observar um efeito remoto."""


class ObservationDecision(Enum):
    """Decisão pura sobre uma leitura remota já realizada."""

    CONVERGED = "CONVERGED"
    RETRY = "RETRY"


class ConvergencePoller:
    """Repete somente leituras; a mutação deve ocorrer antes desta chamada."""

    def __init__(
        self,
        config: ConvergenceConfig,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.monotonic = monotonic
        self.sleep = sleep

    def wait(
        self,
        read: Callable[[], T],
        classify: Callable[[T], ObservationDecision],
        description: str,
    ) -> T:
        """Retorna a primeira leitura convergente ou falha sem ocultar erros."""
        started_at = self.monotonic()
        deadline = started_at + self.config.timeout_seconds
        first_query = True
        while True:
            if not first_query and self.monotonic() >= deadline:
                elapsed = self.monotonic() - started_at
                raise ConvergenceError(
                    f"Timeout ao aguardar {description}: {elapsed:.1f}s "
                    f"(limite de {self.config.timeout_seconds:g}s)"
                )
            try:
                observed = read()
            except Exception as error:
                raise ConvergenceError(
                    f"Falha ao consultar {description}: {error}"
                ) from error
            first_query = False
            decision = classify(observed)
            if decision is ObservationDecision.CONVERGED:
                return observed
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                elapsed = self.monotonic() - started_at
                raise ConvergenceError(
                    f"Timeout ao aguardar {description}: {elapsed:.1f}s "
                    f"(limite de {self.config.timeout_seconds:g}s)"
                )
            self.sleep(min(self.config.poll_interval_seconds, remaining))
