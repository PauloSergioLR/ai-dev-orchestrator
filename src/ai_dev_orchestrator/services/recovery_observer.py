"""Coleta read-only dos fatos externos usados pelo RecoveryPlanner."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_orchestrator.adapters.github import GitHubCiAdapter, GitHubProjectAdapter
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import RunRecord
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, ProjectState,
    PullRequestObservation, PullRequestState, RecoveryObservation, WorktreeState,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.infrastructure.process import CommandRunner
from ai_dev_orchestrator.services.ci_gate import classify_required_checks


class RecoveryObserver:
    """Snapshot read-only; falhas de leitura viram estado desconhecido ou divergente."""

    def __init__(self, config: OrchestratorConfig, store: SqliteExecutionStore,
                 runner: CommandRunner | None = None) -> None:
        self.config, self.store = config, store
        self.runner = runner or CommandRunner(timeout=30)
        self.ci_reader = GitHubCiAdapter(config, self.runner)
        self.projects = GitHubProjectAdapter(config, self.runner)

    def observe(self, run: RunRecord) -> RecoveryObservation:
        worktree, head, parent, dirty = self._worktree(run)
        prs = self._pull_requests(run.branch)
        ci = self._ci(run, prs)
        merge = self._merge(run, prs)
        project = self._project(run)
        remote = self._git(run, ["rev-parse", "--verify", f"{self.config.workspace.remote_name}/{run.branch}"]) if run.branch else None
        return RecoveryObservation(
            worktree, local_head_sha=head, local_head_parent_sha=parent,
            has_worktree_changes=dirty, remote_head_sha=remote,
            pull_requests=prs, ci=ci,
            findings_head_sha=run.reviewed_head_sha if self.store.review_findings(run.id, run.reviewed_head_sha) else None,
            merge=merge, project_state=project,
        )

    def _worktree(self, run: RunRecord) -> tuple[WorktreeState, str | None, str | None, bool]:
        if not run.worktree_path or not Path(run.worktree_path).is_dir():
            return WorktreeState.ABSENT, None, None, False
        branch = self._git(run, ["branch", "--show-current"])
        root = self._git(run, ["rev-parse", "--show-toplevel"])
        if not branch or branch != run.branch or not root:
            return WorktreeState.DIVERGENT, None, None, False
        head = self._git(run, ["rev-parse", "HEAD"])
        parent = self._git(run, ["rev-parse", "HEAD^"])
        status = self._git(run, ["status", "--porcelain", "--untracked-files=all"], raw=True)
        return WorktreeState.CONVERGENT, head, parent, bool(status and status.strip())

    def _git(self, run: RunRecord, arguments: list[str], raw: bool = False) -> str | None:
        if not run.worktree_path:
            return None
        result = self.runner.run(["git", "-C", run.worktree_path, *arguments])
        if result.error or not result.succeeded:
            return None
        return result.stdout if raw else result.stdout.strip() or None

    def _pull_requests(self, branch: str | None) -> tuple[PullRequestObservation, ...]:
        if not branch:
            return ()
        result = self.runner.run(["gh", "pr", "list", "--repo", self.config.github.repository_full_name,
                                  "--head", branch, "--state", "all", "--json",
                                  "number,url,state,baseRefName,headRefName,headRefOid,isDraft,mergedAt,mergeCommit"])
        if result.error or not result.succeeded:
            return ()
        try:
            values = json.loads(result.stdout)
            return tuple(PullRequestObservation(
                value["number"], value["url"], self.config.github.repository_full_name,
                value["baseRefName"], value["headRefName"], value["headRefOid"],
                PullRequestState.MERGED if value.get("mergedAt") else PullRequestState(value["state"]),
            ) for value in values)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return ()

    def _ci(self, run: RunRecord, prs: tuple[PullRequestObservation, ...]) -> CiObservation:
        if len(prs) != 1:
            return CiObservation()
        try:
            snapshot = self.ci_reader.get_ci_snapshot(prs[0].number)
            state, _ = classify_required_checks(snapshot.checks, self.config.ci.required_checks)
            return CiObservation(CiState(state.value), snapshot.head_sha)
        except Exception:
            return CiObservation()

    def _merge(self, run: RunRecord, prs: tuple[PullRequestObservation, ...]) -> MergeObservation:
        if len(prs) != 1:
            return MergeObservation(MergeState.UNKNOWN)
        if prs[0].state == PullRequestState.OPEN:
            return MergeObservation(MergeState.OPEN)
        if prs[0].state != PullRequestState.MERGED:
            return MergeObservation(MergeState.CLOSED)
        result = self.runner.run(["gh", "pr", "view", str(prs[0].number), "--repo",
                                  self.config.github.repository_full_name, "--json", "headRefOid,mergeCommit,mergedAt"])
        try:
            value = json.loads(result.stdout)
            commit = value.get("mergeCommit") or {}
            sha = commit.get("oid")
            if result.succeeded and value.get("mergedAt") and sha and value.get("headRefOid"):
                return MergeObservation(MergeState.MERGED, value["headRefOid"], sha)
        except (TypeError, KeyError, json.JSONDecodeError):
            pass
        return MergeObservation(MergeState.UNKNOWN)

    def _project(self, run: RunRecord) -> ProjectState:
        if not run.project_item_id:
            return ProjectState.UNKNOWN
        try:
            matches = [item for item in self.projects.list_items() if item.id == run.project_item_id]
            if len(matches) != 1 or matches[0].status is None:
                return ProjectState.UNKNOWN
            return ProjectState.DONE if matches[0].status == "Done" else ProjectState.NOT_DONE
        except Exception:
            return ProjectState.UNKNOWN
