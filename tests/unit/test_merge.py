"""Testes da política determinística de auto-merge."""

from __future__ import annotations

import pytest

from ai_dev_orchestrator.domain.ci import CiResult, CiStatus, StatusCheck
from ai_dev_orchestrator.domain.review import ReviewVerdict, StructuredReview
from ai_dev_orchestrator.services.merge import MergeGate, MergeGateError, MergePullRequestSnapshot


SHA_A = "a" * 40


def snapshot(**changes: object) -> MergePullRequestSnapshot:
    values = dict(number=12, url="https://github.com/acme/repo/pull/12", state="OPEN",
                  is_draft=False, base="main", head_branch="feat/merge", head_sha=SHA_A,
                  mergeable="MERGEABLE")
    values.update(changes)
    return MergePullRequestSnapshot(**values)  # type: ignore[arg-type]


def approved() -> StructuredReview:
    return StructuredReview(ReviewVerdict.APPROVED, (), SHA_A, "ok")


def validate(observed: MergePullRequestSnapshot | None = None, local_head: str = SHA_A) -> None:
    MergeGate().validate(observed or snapshot(), pull_request_number=12,
                        pull_request_url="https://github.com/acme/repo/pull/12", base="main",
                        branch="feat/merge", local_head=local_head, review=approved(),
                        ci_result=CiResult(SHA_A, (StatusCheck("test", "COMPLETED", "SUCCESS"),), CiStatus.SUCCESS),
                        blocking_severities=("CRITICAL", "HIGH", "MEDIUM"))


def test_accepts_only_the_exact_approved_state() -> None:
    validate()


@pytest.mark.parametrize("changes", [
    {"head_sha": "b" * 40}, {"state": "CLOSED"}, {"is_draft": True},
    {"base": "release"}, {"head_branch": "other"}, {"mergeable": "CONFLICTING"},
])
def test_refuses_pr_races(changes: dict[str, object]) -> None:
    with pytest.raises(MergeGateError):
        validate(snapshot(**changes))


def test_refuses_changed_local_head() -> None:
    with pytest.raises(MergeGateError):
        validate(local_head="b" * 40)
