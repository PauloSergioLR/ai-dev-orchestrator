"""Implementação concreta de RecoveryEffects usando os adapters existentes."""

from __future__ import annotations

from pathlib import Path

from ai_dev_orchestrator.adapters.codex import CodexAdapter
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.adapters.github import (
    GitHubIssueAdapter, GitHubProjectStatusAdapter, GitHubPullRequestAdapter,
    PullRequest,
)
from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.adapters.publication import GitPublicationAdapter
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, PullRequestObservation,
    PullRequestState,
)
from ai_dev_orchestrator.domain.review import ReviewFinding, ReviewVerdict, StructuredReview
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.services.pipeline import RunPipeline, build_initial_prompt
from ai_dev_orchestrator.services.recovery_executor import CommitResult
from ai_dev_orchestrator.services.validation import LocalValidationService
from ai_dev_orchestrator.services.merge import MergeGate
from ai_dev_orchestrator.services.review import CorrectionContextBuilder


class RecoveryEffects:
    """Ponte de alto nível; não decide próximas ações nem persiste checkpoints."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.worktrees = GitWorktreeAdapter()
        self.codex = CodexAdapter()
        self.validation = LocalValidationService()
        self.publication = GitPublicationAdapter()
        self.issues = GitHubIssueAdapter(config)
        self.pull_requests = GitHubPullRequestAdapter(config)
        self.projects = GitHubProjectStatusAdapter(config)
        self.reviewer = AntigravityAdapter(config.review.timeout_seconds)

    def prepare_worktree(self, run: RunRecord) -> str:
        if not run.branch or not run.worktree_path or not run.base_ref:
            raise ValueError("Identidade do worktree ausente")
        worktree = self.worktrees.create_worktree(self.config.workspace.repository_path, run.branch, run.worktree_path, run.base_ref)
        return self.publication.current_head(worktree.path)

    def start_codex(self, run: RunRecord) -> str:
        issue = self.issues.get_issue(run.issue_number)
        return self.codex.execute(run.worktree_path or "", build_initial_prompt(issue)).session_id

    def resume_codex(self, run: RunRecord) -> str:
        return self.codex.resume(run.worktree_path or "", run.codex_session_id or "", f"Continue a Issue #{run.issue_number} no mesmo worktree.").session_id

    def run_local_gates(self, run: RunRecord) -> None:
        self.validation.validate(run.worktree_path or "")

    def create_commit(self, run: RunRecord) -> CommitResult:
        parent = self.publication.current_head(run.worktree_path or "")
        head = self.publication.commit_correction(run.worktree_path or "") if run.pull_request_number else self.publication.commit(run.worktree_path or "", run.issue_number)
        return CommitResult(head, parent)

    def push_branch(self, run: RunRecord) -> None:
        self.publication.push(run.worktree_path or "", self.config.workspace.remote_name, run.branch or "")

    def create_pull_request(self, run: RunRecord) -> PullRequestObservation:
        issue = self.issues.get_issue(run.issue_number)
        gates = self.validation.validate(run.worktree_path or "")
        created = self.pull_requests.create(issue, run.branch or "", gates)
        return PullRequestObservation(created.number, created.url, self.config.github.repository_full_name, created.base, created.head, self.publication.current_head(run.worktree_path or ""), PullRequestState.OPEN)

    def wait_for_ci(self, run: RunRecord) -> CiObservation:
        result = self._wait_ci_result(run)
        return CiObservation(CiState(result.status.value), result.expected_head_sha)

    def _wait_ci_result(self, run: RunRecord):
        from ai_dev_orchestrator.adapters.github import GitHubCiAdapter
        from ai_dev_orchestrator.services.ci_gate import CiGate
        return CiGate(GitHubCiAdapter(self.config), self.config.ci).wait(run.pull_request_number or 0, run.current_head_sha or "")

    def review_head(self, run: RunRecord, prior_findings: tuple[ReviewFinding, ...]) -> StructuredReview:
        if not run.pull_request_number or not run.pull_request_url or not run.current_head_sha:
            raise ValueError("Identidade de review incompleta")
        issue = self.issues.get_issue(run.issue_number)
        gates = self.validation.validate(run.worktree_path or "")
        ci_result = self._wait_ci_result(run)
        pipeline = RunPipeline(self.config, self.issues, self.projects, self.projects,
                               self.worktrees, self.codex, self.validation, self.publication,
                               self.pull_requests, self.pull_requests, self.pull_requests,
                               self.reviewer, self.pull_requests)
        worktree = GitWorktree(self.config.workspace.repository_path, Path(run.worktree_path or ""), run.branch or "", run.base_ref or "")
        pull = PullRequest(run.pull_request_number, run.pull_request_url, issue.title, self.config.github.pull_request_base, run.branch or "")
        return pipeline._review_head(issue, worktree, pull, run.current_head_sha, gates, ci_result, prior_findings)

    def resume_correction(self, run: RunRecord, findings: tuple[ReviewFinding, ...]) -> str:
        if not findings or not run.pull_request_number or not run.pull_request_url or not run.reviewed_head_sha or not run.codex_session_id:
            raise ValueError("Contexto de correção incompleto")
        issue = self.issues.get_issue(run.issue_number)
        rejected = StructuredReview(ReviewVerdict.REJECTED, findings, run.reviewed_head_sha, "Findings persistidos")
        prompt = CorrectionContextBuilder().build(issue, run.pull_request_number, run.pull_request_url, run.reviewed_head_sha, rejected, ())
        return self.codex.resume(run.worktree_path or "", run.codex_session_id or "", prompt).session_id

    def merge_pull_request(self, run: RunRecord) -> MergeObservation:
        if not run.pull_request_number or not run.pull_request_url or not run.reviewed_head_sha:
            raise ValueError("Identidade de merge incompleta")
        branch, local_head = self.publication.merge_state(run.worktree_path or "")
        snapshot = self.pull_requests.get_merge_snapshot(run.pull_request_number)
        ci_result = self._wait_ci_result(run)
        review = StructuredReview(ReviewVerdict.APPROVED, (), run.reviewed_head_sha, "Review persistida")
        MergeGate().validate(snapshot, pull_request_number=run.pull_request_number,
                            pull_request_url=run.pull_request_url, base=self.config.github.pull_request_base,
                            branch=branch, local_head=local_head, review=review,
                            ci_result=ci_result,
                            blocking_severities=self.config.review.blocking_severities)
        result = self.pull_requests.merge(run.pull_request_number, run.reviewed_head_sha)
        confirmed = self.pull_requests.get_merge_snapshot(run.pull_request_number)
        if not confirmed.merged or confirmed.head_sha != run.reviewed_head_sha or confirmed.merge_commit_sha != result.merge_commit_sha:
            raise ValueError("GitHub não confirmou merge do HEAD aprovado")
        self.pull_requests.verify_merge_commit(result.merge_commit_sha, result.merged_head_sha)
        return MergeObservation(MergeState.MERGED, result.merged_head_sha, result.merge_commit_sha)

    def mark_project_done(self, run: RunRecord) -> None:
        self.projects.set_status(run.project_item_id or "", "Done")
