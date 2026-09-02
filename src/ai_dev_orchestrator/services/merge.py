"""Política fail-closed para o merge automático de Pull Requests."""

from __future__ import annotations

from dataclasses import dataclass
import re

from ai_dev_orchestrator.domain.ci import CiResult, CiStatus
from ai_dev_orchestrator.domain.review import ReviewVerdict, StructuredReview


class MergeGateError(Exception):
    """Indica que o estado observado não autoriza um merge automático."""


@dataclass(frozen=True)
class MergePullRequestSnapshot:
    number: int
    url: str
    state: str
    is_draft: bool
    base: str
    head_branch: str
    head_sha: str
    mergeable: str
    merged: bool = False
    merge_commit_sha: str = ""


@dataclass(frozen=True)
class MergeResult:
    merged_head_sha: str
    merge_commit_sha: str


class MergeGate:
    """Centraliza as invariantes que precisam convergir antes da mutação remota."""

    def validate(
        self, snapshot: MergePullRequestSnapshot, *, pull_request_number: int,
        pull_request_url: str, base: str, branch: str, local_head: str,
        review: StructuredReview, ci_result: CiResult,
        blocking_severities: tuple[str, ...],
    ) -> None:
        if review.verdict is not ReviewVerdict.APPROVED:
            raise MergeGateError("Review não foi aprovado")
        if any(f.severity.value in blocking_severities for f in review.findings):
            raise MergeGateError("Review aprovado contém finding bloqueante")
        if ci_result.status is not CiStatus.SUCCESS:
            raise MergeGateError("CI não está em SUCCESS")
        if (snapshot.number != pull_request_number or snapshot.url != pull_request_url
                or snapshot.state != "OPEN" or snapshot.is_draft
                or snapshot.base != base or snapshot.head_branch != branch
                or snapshot.mergeable != "MERGEABLE"):
            raise MergeGateError("Pull Request não corresponde ao estado final esperado")
        approved_sha = review.reviewed_head_sha
        if not _is_sha(approved_sha):
            raise MergeGateError("SHA aprovado pelo reviewer é inválido")
        if not (snapshot.head_sha == approved_sha == ci_result.expected_head_sha == local_head):
            raise MergeGateError("HEAD do PR, CI, review e worktree não convergem")


def _is_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value))
