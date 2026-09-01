"""Testes da política determinística de CI sem GitHub ou sleep real."""

from dataclasses import dataclass, field

import pytest

from ai_dev_orchestrator.config import CiConfig
from ai_dev_orchestrator.domain.ci import CiStatus, PullRequestCiSnapshot, StatusCheck
from ai_dev_orchestrator.services.ci_gate import CiGate, CiGateError, classify_required_checks


SHA = "a" * 40


def check(name: str = "test", status: str = "COMPLETED", conclusion: str | None = "SUCCESS") -> StatusCheck:
    return StatusCheck(name, status, conclusion, "https://ci.example/check")


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ((check(),), CiStatus.SUCCESS),
        ((), CiStatus.PENDING),
        ((check(status="QUEUED", conclusion=None),), CiStatus.PENDING),
        ((check(status="IN_PROGRESS", conclusion=None),), CiStatus.PENDING),
        ((check(conclusion="FAILURE"),), CiStatus.FAILURE),
        ((check(conclusion="CANCELLED"),), CiStatus.FAILURE),
        ((check(conclusion="TIMED_OUT"),), CiStatus.FAILURE),
        ((check(conclusion="ACTION_REQUIRED"),), CiStatus.FAILURE),
        ((check(conclusion="STALE"),), CiStatus.FAILURE),
        ((check(conclusion="STARTUP_FAILURE"),), CiStatus.FAILURE),
        ((check(conclusion="UNRECOGNIZED"),), CiStatus.FAILURE),
    ],
)
def test_classifies_required_checks_conservatively(
    checks: tuple[StatusCheck, ...], expected: CiStatus
) -> None:
    assert classify_required_checks(checks, ("test",))[0] is expected


def test_optional_check_and_ambiguous_duplicate_do_not_create_false_result() -> None:
    assert classify_required_checks((check("optional", conclusion="FAILURE"), check()), ("test",))[0] is CiStatus.SUCCESS
    assert classify_required_checks((check(), check()), ("test",))[0] is CiStatus.PENDING


@dataclass
class Reader:
    snapshots: list[PullRequestCiSnapshot]
    calls: list[int] = field(default_factory=list)

    def get_ci_snapshot(self, number: int) -> PullRequestCiSnapshot:
        self.calls.append(number)
        return self.snapshots.pop(0)


@dataclass
class FakeTime:
    value: float = 0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def gate(reader: Reader, clock: FakeTime, timeout: float = 20) -> CiGate:
    return CiGate(reader, CiConfig(required_checks=("test",), poll_interval_seconds=5, timeout_seconds=timeout), clock.monotonic, clock.sleep)


def test_polls_after_immediate_query_and_returns_success_without_extra_sleep() -> None:
    reader = Reader([PullRequestCiSnapshot(SHA, (check(status="PENDING", conclusion=None),)), PullRequestCiSnapshot(SHA, (check(),))])
    clock = FakeTime()
    result = gate(reader, clock).wait(42)
    assert result.status is CiStatus.SUCCESS
    assert result.expected_head_sha == SHA
    assert reader.calls == [42, 42]
    assert clock.sleeps == [5]


def test_failure_stops_immediately_and_includes_check_details() -> None:
    reader = Reader([PullRequestCiSnapshot(SHA, (check(conclusion="FAILURE"),))])
    clock = FakeTime()
    with pytest.raises(CiGateError, match=r"test.*FAILURE.*ci.example"):
        gate(reader, clock).wait(42)
    assert reader.calls == [42]
    assert clock.sleeps == []


def test_timeout_uses_injected_clock_and_never_sleeps_past_deadline() -> None:
    reader = Reader([PullRequestCiSnapshot(SHA, ()) for _ in range(3)])
    clock = FakeTime()
    with pytest.raises(CiGateError, match="permaneceu pendente"):
        gate(reader, clock, timeout=10).wait(42)
    assert clock.sleeps == [5, 5]


def test_changed_head_is_rejected_before_old_check_can_approve() -> None:
    reader = Reader([
        PullRequestCiSnapshot(SHA, (check(status="PENDING", conclusion=None),)),
        PullRequestCiSnapshot("b" * 40, (check(),)),
    ])
    clock = FakeTime()
    with pytest.raises(CiGateError, match=r"mudou.*a{40}.*b{40}"):
        gate(reader, clock).wait(42)
    assert reader.calls == [42, 42]
