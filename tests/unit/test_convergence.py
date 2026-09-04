"""Testes do polling de consistência eventual sem relógio ou rede reais."""

from dataclasses import dataclass, field

import pytest

from ai_dev_orchestrator.config import ConvergenceConfig
from ai_dev_orchestrator.services.convergence import (
    ConvergenceError,
    ConvergencePoller,
    ObservationDecision,
)
from ai_dev_orchestrator.services.merge import (
    MergeGateError,
    MergePullRequestSnapshot,
    wait_for_merge_confirmation,
)


HEAD = "a" * 40
MERGE = "b" * 40
URL = "https://github.com/acme/repo/pull/40"


@dataclass
class FakeTime:
    value: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def poller(clock: FakeTime, timeout: float = 3) -> ConvergencePoller:
    return ConvergencePoller(
        ConvergenceConfig(poll_interval_seconds=1, timeout_seconds=timeout),
        clock.monotonic,
        clock.sleep,
    )


def merge_snapshot(state: str, commit: str = "") -> MergePullRequestSnapshot:
    return MergePullRequestSnapshot(
        40, URL, state, False, "main", "fix/convergence", HEAD, "UNKNOWN",
        state == "MERGED", commit,
    )


def test_merge_open_then_converges_without_repeating_mutation() -> None:
    clock = FakeTime()
    snapshots = iter((merge_snapshot("OPEN"), merge_snapshot("OPEN"),
                      merge_snapshot("MERGED", MERGE)))
    reads: list[str] = []

    result = wait_for_merge_confirmation(
        poller(clock),
        lambda: (reads.append("read") or next(snapshots)),
        pull_request_number=40,
        pull_request_url=URL,
        expected_head_sha=HEAD,
        expected_merge_commit_sha=MERGE,
    )

    assert result.merge_commit_sha == MERGE
    assert reads == ["read", "read", "read"]
    assert clock.sleeps == [1, 1]


def test_timeout_is_controlled_and_bounded() -> None:
    clock = FakeTime()
    with pytest.raises(ConvergenceError, match=r"Timeout.*3\.0s.*limite de 3s"):
        poller(clock).wait(
            lambda: "anterior",
            lambda _value: ObservationDecision.RETRY,
            "estado remoto",
        )
    assert clock.sleeps == [1, 1, 1]


def test_real_merge_divergence_fails_immediately() -> None:
    clock = FakeTime()
    divergent = MergePullRequestSnapshot(
        40, URL, "MERGED", False, "main", "fix/convergence", "c" * 40,
        "UNKNOWN", True, MERGE,
    )
    with pytest.raises(MergeGateError, match="Identidade"):
        wait_for_merge_confirmation(
            poller(clock), lambda: divergent,
            pull_request_number=40, pull_request_url=URL,
            expected_head_sha=HEAD, expected_merge_commit_sha=MERGE,
        )
    assert clock.sleeps == []


def test_read_error_is_not_converted_to_absence() -> None:
    clock = FakeTime()

    def fail() -> str:
        raise RuntimeError("GitHub indisponível")

    with pytest.raises(ConvergenceError, match="Falha ao consultar") as error:
        poller(clock).wait(
            fail, lambda _value: ObservationDecision.CONVERGED, "Pull Request"
        )
    assert isinstance(error.value.__cause__, RuntimeError)
    assert clock.sleeps == []
