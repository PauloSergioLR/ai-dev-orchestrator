"""Modelos estritos da revisão técnica independente."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewVerdict(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FindingSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True)
class ReviewFinding:
    severity: FindingSeverity
    title: str
    description: str
    path: str | None = None
    line: int | None = None
    criterion: str | None = None


@dataclass(frozen=True)
class StructuredReview:
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...]
    reviewed_head_sha: str
    summary: str


@dataclass(frozen=True)
class ReviewPlan:
    risks: tuple[str, ...]
    invariants: tuple[str, ...]
    acceptance_evidence: tuple[str, ...]
    side_effects: tuple[str, ...]
    regressions: tuple[str, ...]
    tests: tuple[str, ...]
    security_risks: tuple[str, ...]
    architecture_points: tuple[str, ...]


@dataclass(frozen=True)
class ReviewDossier:
    issue_number: int
    issue_title: str
    issue_body: str
    pull_request_number: int
    pull_request_url: str
    base: str
    head_branch: str
    head_sha: str
    commits: tuple[str, ...]
    changed_files: tuple[str, ...]
    diff: str
    repository_rules: str
    local_gates: tuple[str, ...]
    ci_checks: tuple[str, ...]
    ci_status: str
    prior_findings: tuple[ReviewFinding, ...] = ()
