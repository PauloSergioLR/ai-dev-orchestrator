"""Política determinística para aguardar a CI do HEAD exato de um PR."""

from __future__ import annotations

import time
from typing import Callable, Protocol

from ai_dev_orchestrator.config import CiConfig
from ai_dev_orchestrator.domain.ci import CiResult, CiStatus, PullRequestCiSnapshot, StatusCheck


class CiGateError(Exception):
    """Indica uma CI reprovada, expirada ou estruturalmente insegura."""


class PullRequestCiReader(Protocol):
    """Consulta estruturada, sem decidir a política de aprovação."""

    def get_ci_snapshot(self, pull_request_number: int) -> PullRequestCiSnapshot: ...


def classify_required_checks(
    checks: tuple[StatusCheck, ...], required_checks: tuple[str, ...]
) -> tuple[CiStatus, StatusCheck | None]:
    """Classifica somente checks exigidos; duplicidade nunca aprova silenciosamente."""
    by_name: dict[str, list[StatusCheck]] = {}
    for check in checks:
        by_name.setdefault(check.name, []).append(check)
    for name in required_checks:
        observed = by_name.get(name, [])
        if not observed:
            return CiStatus.PENDING, None
        for check in observed:
            classification = _classify_check(check)
            if classification is CiStatus.FAILURE:
                return CiStatus.FAILURE, check
        if len(observed) != 1:
            return CiStatus.PENDING, None
        if _classify_check(observed[0]) is CiStatus.PENDING:
            return CiStatus.PENDING, None
    return CiStatus.SUCCESS, None


def _classify_check(check: StatusCheck) -> CiStatus:
    status = check.status.upper()
    conclusion = check.conclusion.upper() if check.conclusion is not None else None
    if status in {"QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING"}:
        return CiStatus.PENDING
    if status != "COMPLETED":
        return CiStatus.FAILURE
    return CiStatus.SUCCESS if conclusion == "SUCCESS" else CiStatus.FAILURE


class CiGate:
    """Aguarda a CI usando relógio e sleep injetáveis para testes sem espera real."""

    def __init__(
        self,
        reader: PullRequestCiReader,
        config: CiConfig,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.reader = reader
        self.config = config
        self.monotonic = monotonic
        self.sleep = sleep

    def wait(
        self,
        pull_request_number: int,
        expected_head_sha: str,
        stale_head_sha: str | None = None,
    ) -> CiResult:
        started_at = self.monotonic()
        deadline = started_at + self.config.timeout_seconds
        first_query = True
        while True:
            if not first_query and self.monotonic() >= deadline:
                raise self._timeout_error(started_at, expected_head_sha)
            try:
                snapshot = self.reader.get_ci_snapshot(pull_request_number)
            except Exception as error:
                raise CiGateError(
                    f"Falha ao consultar a CI do Pull Request #{pull_request_number} "
                    f"para o HEAD esperado {expected_head_sha}: {error}"
                ) from error
            first_query = False
            if snapshot.head_sha != expected_head_sha:
                if stale_head_sha is not None and snapshot.head_sha == stale_head_sha:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise self._timeout_error(started_at, expected_head_sha)
                    self.sleep(min(self.config.poll_interval_seconds, remaining))
                    continue
                raise CiGateError(
                    f"O HEAD do Pull Request mudou de {expected_head_sha} para {snapshot.head_sha}"
                )
            status, failed_check = classify_required_checks(
                snapshot.checks, self.config.required_checks
            )
            required_observed = tuple(
                check for check in snapshot.checks if check.name in self.config.required_checks
            )
            result = CiResult(expected_head_sha, required_observed, status)
            if status is CiStatus.SUCCESS:
                return result
            if status is CiStatus.FAILURE:
                assert failed_check is not None
                details = f"; detalhes: {failed_check.details_url}" if failed_check.details_url else ""
                raise CiGateError(
                    f"Check obrigatório '{failed_check.name}' terminou com status "
                    f"'{failed_check.status}' e conclusão '{failed_check.conclusion}' "
                    f"para o HEAD esperado {expected_head_sha}{details}"
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise self._timeout_error(started_at, expected_head_sha)
            self.sleep(min(self.config.poll_interval_seconds, remaining))

    def _timeout_error(self, started_at: float, expected_head_sha: str) -> CiGateError:
        elapsed = self.monotonic() - started_at
        return CiGateError(
            f"A CI permaneceu pendente por {elapsed:.1f}s (limite de "
            f"{self.config.timeout_seconds:g}s) para o HEAD {expected_head_sha}"
        )
