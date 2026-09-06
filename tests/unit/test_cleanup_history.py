from dataclasses import dataclass, field
from pathlib import Path

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.cleanup import CleanupService
from ai_dev_orchestrator.services.history import HistoryService


def config(tmp_path: Path, **cleanup: bool) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "o", "repository": "r", "project_number": 1, "ready_status": "Ready", "pull_request_target": "main", "protected_branches": ("main",)},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        workspace={"repository_path": tmp_path, "worktrees_dir": tmp_path / "worktrees", "base_branch": "main"},
        state={"database_path": tmp_path / "state.db"}, cleanup=cleanup,
    )


def completed(store: SqliteExecutionStore, branch="work/x", path="C:/work/x", merged=False):
    run = store.create(46, branch=branch, worktree_path=path, base_ref="main")
    for phase in (ExecutionPhase.CODEX_RUNNING, ExecutionPhase.TESTING, ExecutionPhase.COMMIT_PENDING,
                  ExecutionPhase.PUSH_PENDING, ExecutionPhase.PR_PENDING, ExecutionPhase.WAITING_CI,
                  ExecutionPhase.GEMINI_REVIEWING, ExecutionPhase.MERGE_PENDING,
                  ExecutionPhase.PROJECT_DONE_PENDING, ExecutionPhase.COMPLETED):
        updates = {}
        if phase is ExecutionPhase.COMPLETED and merged:
            updates = {
                "pull_request_number": 46,
                "current_head_sha": "a" * 40,
                "merged_head_sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
            }
        run = store.transition(run.id, phase, summary=phase.value, **updates)
    return run


@dataclass
class FakeGit:
    clean: bool = True
    local: bool = True
    remote: bool = True
    calls: list[str] = field(default_factory=list)
    def worktree_is_clean(self, *_): return self.clean
    def remove_worktree(self, *_): self.calls.append("worktree")
    def local_branch_exists(self, *_): return self.local
    def delete_local_branch(self, *_):
        self.calls.append("local")
        self.local = False
    def remote_branch_exists(self, *_): return self.remote
    def delete_remote_branch(self, *_):
        self.calls.append("remote")
        self.remote = False


def test_clean_completed_worktree_is_removed_and_repeated_cleanup_is_safe(tmp_path: Path) -> None:
    store, git = SqliteExecutionStore(tmp_path / "state.db"), FakeGit()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run = completed(store, path=str(worktree))
    service = CleanupService(config(tmp_path), store, git)
    assert service.cleanup(run.id).status == "DONE"
    assert git.calls == ["worktree"]
    assert service.cleanup(run.id).status == "DONE"
    assert git.calls == ["worktree"]


def test_dirty_or_noncompleted_execution_is_preserved(tmp_path: Path) -> None:
    store, git = SqliteExecutionStore(tmp_path / "state.db"), FakeGit(clean=False)
    worktree = tmp_path / "dirty-worktree"
    worktree.mkdir()
    run = completed(store, path=str(worktree))
    assert CleanupService(config(tmp_path), store, git).cleanup(run.id).status == "PENDING"
    active = store.create(47, branch="work/y", worktree_path="C:/work/y")
    assert CleanupService(config(tmp_path), store, git).cleanup(active.id).status == "PRESERVED"
    assert not git.calls


def test_protected_branch_and_unproven_remote_are_never_removed(tmp_path: Path) -> None:
    store, git = SqliteExecutionStore(tmp_path / "state.db"), FakeGit()
    protected = completed(store, branch="main")
    assert CleanupService(config(tmp_path, remove_local_branch=True), store, git).cleanup(protected.id).status == "PRESERVED"
    store2, git2 = SqliteExecutionStore(tmp_path / "other.db"), FakeGit()
    run = completed(store2)
    assert CleanupService(config(tmp_path, remove_remote_branch=True), store2, git2).cleanup(run.id).status == "PENDING"
    assert "remote" not in git2.calls


def test_remote_branch_is_removed_only_after_persisted_merge_of_expected_head(tmp_path: Path) -> None:
    store, git = SqliteExecutionStore(tmp_path / "state.db"), FakeGit()
    run = completed(store, merged=True)

    result = CleanupService(config(tmp_path, remove_remote_branch=True), store, git).cleanup(run.id)

    assert result.status == "DONE"
    assert git.calls == ["remote"]


def test_waiting_quota_and_human_required_are_preserved(tmp_path: Path) -> None:
    store, git = SqliteExecutionStore(tmp_path / "state.db"), FakeGit()
    quota = store.create(46, branch="work/quota", worktree_path="C:/work/quota")
    quota = store.transition(quota.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    quota = store.transition(quota.id, ExecutionPhase.WAITING_CODEX_QUOTA, summary="Quota")
    human = store.create(47, branch="work/human", worktree_path="C:/work/human")
    for phase in (ExecutionPhase.CODEX_RUNNING, ExecutionPhase.TESTING, ExecutionPhase.COMMIT_PENDING,
                  ExecutionPhase.PUSH_PENDING, ExecutionPhase.PR_PENDING, ExecutionPhase.WAITING_CI,
                  ExecutionPhase.GEMINI_REVIEWING, ExecutionPhase.APPROVED_AWAITING_ACTION):
        human = store.transition(human.id, phase, summary=phase.value)

    service = CleanupService(config(tmp_path), store, git)
    assert service.cleanup(quota.id).status == "PRESERVED"
    assert service.cleanup(human.id).status == "PRESERVED"
    assert not git.calls


def test_cleanup_failure_keeps_completed_and_does_not_change_duration(tmp_path: Path) -> None:
    class FailingGit(FakeGit):
        def remove_worktree(self, *_):
            raise RuntimeError("Git indisponível")

    store, git = SqliteExecutionStore(tmp_path / "state.db"), FailingGit()
    worktree = tmp_path / "failing-worktree"
    worktree.mkdir()
    run = completed(store, path=str(worktree))
    before = HistoryService(store).metrics(run).duration

    result = CleanupService(config(tmp_path), store, git).cleanup(run.id)
    persisted = store.get(run.id)

    assert result.status == "PENDING"
    assert persisted.phase is ExecutionPhase.COMPLETED
    assert HistoryService(store).metrics(persisted).duration == before


def test_history_and_structured_usage_are_derived_without_provider_content(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    first = completed(store)
    store.record_usage(first.id, "codex", tokens=12, cost=0.5)
    store.create(47)
    history = HistoryService(store).list()
    assert [item.run.issue_number for item in history] == [47, 46]
    assert history[1].run.codex_tokens == 12
    assert history[1].run.gemini_tokens is None
    assert history[1].reviews == 0
