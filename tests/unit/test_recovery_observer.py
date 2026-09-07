"""Observação de recovery sem rede nem processos reais."""

from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.ci import PullRequestCiSnapshot, StatusCheck
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.recovery import (
    MergeState,
    ProjectState,
    PullRequestObservation,
    PullRequestState,
    RecoveryAction,
    RecoveryObservation,
    RecoveryPolicy,
    WorktreeState,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.recovery_observer import (
    RecoveryObservationError,
    RecoveryObserver,
)
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner

HEAD = "a" * 40
PARENT = "b" * 40
MERGE = "c" * 40


class Runner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: list[str]) -> CommandResult:
        key = tuple(str(value) for value in arguments)
        self.calls.append(key)
        return self.results.get(key, CommandResult(1, stderr="comando não previsto"))


def config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "owner", "repository": "repo", "project_number": 1,
                "ready_status": "Ready", "pull_request_base": "main"},
        workspace={"repository_path": tmp_path / "repo",
                   "worktrees_dir": tmp_path / "worktrees",
                   "base_ref": "origin/main", "remote_name": "upstream"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": True},
        state={"database_path": tmp_path / "state.db"},
    )


def run(tmp_path: Path, **changes: object) -> RunRecord:
    now = datetime(2026, 1, 1)
    values: dict[str, object] = {
        "id": "execution", "issue_number": 37, "phase": ExecutionPhase.PUSH_PENDING,
        "created_at": now, "updated_at": now, "branch": "feat/recovery",
        "worktree_path": str(tmp_path / "worktree"), "base_ref": "origin/main",
        "current_head_sha": HEAD,
    }
    values.update(changes)
    return RunRecord(**values)  # type: ignore[arg-type]


def observer(tmp_path: Path, results: dict[tuple[str, ...], CommandResult]) -> RecoveryObserver:
    cfg = config(tmp_path)
    return RecoveryObserver(cfg, SqliteExecutionStore(cfg.state.database_path), Runner(results))


def worktree_results(tmp_path: Path, *, branch: str = "feat/recovery",
                     common: Path | None = None, status: str = "") -> dict[tuple[str, ...], CommandResult]:
    worktree = str(tmp_path / "worktree")
    repository = str(tmp_path / "repo")
    common_dir = common or (tmp_path / "repo" / ".git")
    return {
        ("git", "-C", worktree, "branch", "--show-current"): CommandResult(0, branch),
        ("git", "-C", worktree, "rev-parse", "--show-toplevel"): CommandResult(0, worktree),
        ("git", "-C", worktree, "rev-parse", "--git-common-dir"): CommandResult(0, str(common_dir)),
        ("git", "-C", repository, "rev-parse", "--git-common-dir"): CommandResult(0, str(tmp_path / "repo" / ".git")),
        ("git", "-C", worktree, "rev-parse", "HEAD"): CommandResult(0, HEAD),
        ("git", "-C", worktree, "rev-parse", "HEAD^"): CommandResult(0, PARENT),
        ("git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"): CommandResult(0, status),
    }


def test_worktree_absent_is_a_fact(tmp_path: Path) -> None:
    assert observer(tmp_path, {})._worktree(run(tmp_path))[0] == WorktreeState.ABSENT


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, WorktreeState.CONVERGENT),
        ({"branch": "outra"}, WorktreeState.DIVERGENT),
        ({"common": Path("C:/outro/.git")}, WorktreeState.DIVERGENT),
    ],
)
def test_worktree_identity_is_proven(
    tmp_path: Path, change: dict[str, object], expected: WorktreeState
) -> None:
    (tmp_path / "worktree").mkdir()
    results = worktree_results(tmp_path, **change)  # type: ignore[arg-type]

    assert observer(tmp_path, results)._worktree(run(tmp_path))[0] == expected


def test_worktree_status_includes_untracked_files(tmp_path: Path) -> None:
    (tmp_path / "worktree").mkdir()
    value = observer(tmp_path, worktree_results(tmp_path, status="?? novo.py\n"))._worktree(run(tmp_path))

    assert value == (WorktreeState.CONVERGENT, HEAD, PARENT, True)


def test_base_ref_divergent_is_not_observed_as_absent(tmp_path: Path) -> None:
    (tmp_path / "worktree").mkdir()

    assert observer(tmp_path, {})._worktree(run(tmp_path, base_ref="outra"))[0] == WorktreeState.DIVERGENT


def test_legacy_remote_base_ref_is_convergent_with_configured_branch(
    tmp_path: Path,
) -> None:
    (tmp_path / "worktree").mkdir()
    cfg = config(tmp_path).model_copy(
        update={
            "workspace": config(tmp_path).workspace.model_copy(
                update={"base_branch": "main", "remote_name": "origin"}
            )
        }
    )
    value = RecoveryObserver(
        cfg,
        SqliteExecutionStore(cfg.state.database_path),
        Runner(worktree_results(tmp_path)),
    )

    observed = value._worktree(
        run(tmp_path, base_ref="refs/remotes/origin/main")
    )

    assert observed == (WorktreeState.CONVERGENT, HEAD, PARENT, False)


def test_gemini_reviewing_with_equivalent_legacy_base_plans_review_head(
    tmp_path: Path,
) -> None:
    (tmp_path / "worktree").mkdir()
    original = config(tmp_path)
    cfg = original.model_copy(
        update={
            "workspace": original.workspace.model_copy(
                update={"base_branch": "main", "remote_name": "origin"}
            )
        }
    )
    value = RecoveryObserver(
        cfg,
        SqliteExecutionStore(cfg.state.database_path),
        Runner(worktree_results(tmp_path)),
    )
    record = run(
        tmp_path,
        phase=ExecutionPhase.GEMINI_REVIEWING,
        base_ref="refs/remotes/origin/main",
        pull_request_number=49,
        pull_request_url="https://github.com/owner/repo/pull/49",
        ci_head_sha=HEAD,
    )
    worktree_state, local_head, parent, dirty = value._worktree(record)
    pull_request = PullRequestObservation(
        49,
        "https://github.com/owner/repo/pull/49",
        "owner/repo",
        "main",
        "feat/recovery",
        HEAD,
        PullRequestState.OPEN,
    )
    snapshot = RecoveryObservation(
        worktree_state,
        local_head_sha=local_head,
        local_head_parent_sha=parent,
        has_worktree_changes=dirty,
        remote_head_sha=HEAD,
        pull_requests=(pull_request,),
    )
    policy = RecoveryPolicy("owner/repo", "main", True, 3)

    decision = RecoveryPlanner(policy).plan(record, snapshot)

    assert decision.action == RecoveryAction.REVIEW_HEAD


def test_remote_head_uses_configured_repository_and_remote(tmp_path: Path) -> None:
    expected = ("git", "-C", str(tmp_path / "repo"), "ls-remote", "--heads",
                "upstream", "refs/heads/feat/recovery")
    runner = Runner({expected: CommandResult(0, f"{HEAD}\trefs/heads/feat/recovery\n")})
    value = RecoveryObserver(config(tmp_path), SqliteExecutionStore(tmp_path / "state.db"), runner)

    assert value._remote_head(run(tmp_path)) == HEAD
    assert runner.calls == [expected]


@pytest.mark.parametrize(
    ("result", "expected"),
    [(CommandResult(0, ""), None), (CommandResult(1, stderr="offline"), "error")],
)
def test_remote_absence_differs_from_read_failure(
    tmp_path: Path, result: CommandResult, expected: str | None
) -> None:
    key = ("git", "-C", str(tmp_path / "repo"), "ls-remote", "--heads",
           "upstream", "refs/heads/feat/recovery")
    value = observer(tmp_path, {key: result})
    if expected == "error":
        with pytest.raises(RecoveryObservationError):
            value._remote_head(run(tmp_path))
    else:
        assert value._remote_head(run(tmp_path)) is None


def test_pr_empty_success_differs_from_invalid_or_failed_query(tmp_path: Path) -> None:
    prefix = ("gh", "pr", "list", "--repo", "owner/repo", "--head", "feat/recovery",
              "--state", "all", "--json",
              "number,url,state,baseRefName,headRefName,headRefOid,isDraft,mergedAt,mergeCommit")
    assert observer(tmp_path, {prefix: CommandResult(0, "[]")})._pull_requests("feat/recovery") == ()
    for result in (CommandResult(1, stderr="offline"), CommandResult(0, "{")):
        with pytest.raises(RecoveryObservationError):
            observer(tmp_path, {prefix: result})._pull_requests("feat/recovery")


def test_pr_query_preserves_all_matches_and_states(tmp_path: Path) -> None:
    key = ("gh", "pr", "list", "--repo", "owner/repo", "--head", "feat/recovery",
           "--state", "all", "--json",
           "number,url,state,baseRefName,headRefName,headRefOid,isDraft,mergedAt,mergeCommit")
    payload = [
        {"number": 1, "url": "u1", "state": "OPEN", "baseRefName": "main",
         "headRefName": "feat/recovery", "headRefOid": HEAD, "mergedAt": None},
        {"number": 2, "url": "u2", "state": "CLOSED", "baseRefName": "main",
         "headRefName": "feat/recovery", "headRefOid": HEAD, "mergedAt": None},
    ]

    assert [item.state.value for item in observer(tmp_path, {key: CommandResult(0, json.dumps(payload))})._pull_requests("feat/recovery")] == ["OPEN", "CLOSED"]


def test_ci_failure_is_not_converted_to_absent(tmp_path: Path) -> None:
    value = observer(tmp_path, {})
    value.ci_reader = SimpleNamespace(get_ci_snapshot=lambda _number: (_ for _ in ()).throw(RuntimeError("offline")))
    pr = value._pull_requests  # keep the assertion focused on the public normalization
    del pr
    from ai_dev_orchestrator.domain.recovery import PullRequestObservation, PullRequestState
    pull = PullRequestObservation(1, "u", "owner/repo", "main", "feat/recovery", HEAD, PullRequestState.OPEN)

    with pytest.raises(RecoveryObservationError):
        value._ci(run(tmp_path), (pull,))


def test_ci_project_and_verified_merge_are_normalized(tmp_path: Path) -> None:
    from ai_dev_orchestrator.domain.recovery import PullRequestObservation, PullRequestState
    value = observer(tmp_path, {})
    pull = PullRequestObservation(1, "u", "owner/repo", "main", "feat/recovery", HEAD, PullRequestState.MERGED)
    value.ci_reader = SimpleNamespace(get_ci_snapshot=lambda _number: PullRequestCiSnapshot(
        HEAD, (StatusCheck("test", "COMPLETED", "SUCCESS", None),)
    ))
    verified: list[tuple[str, str]] = []
    value.pull_requests = SimpleNamespace(
        get_merge_snapshot=lambda _number: SimpleNamespace(
            merged=True, state="MERGED", number=1, url="u", base="main",
            head_branch="feat/recovery", head_sha=HEAD, merge_commit_sha=MERGE,
        ),
        verify_merge_commit=lambda merge, head: verified.append((merge, head)),
    )
    value.projects = SimpleNamespace(list_items=lambda: (SimpleNamespace(id="item", status="Done"),))

    assert value._ci(run(tmp_path), (pull,)).head_sha == HEAD
    assert value._merge(run(tmp_path), (pull,)).state == MergeState.MERGED
    assert verified == [(MERGE, HEAD)]
    assert value._project(run(tmp_path, project_item_id="item")) == ProjectState.DONE


def test_project_unknown_not_done_and_done_are_distinct(tmp_path: Path) -> None:
    value = observer(tmp_path, {})
    value.projects = SimpleNamespace(list_items=lambda: ())
    assert value._project(run(tmp_path, project_item_id="item")) == ProjectState.UNKNOWN
    value.projects = SimpleNamespace(
        list_items=lambda: (SimpleNamespace(id="item", status="In Progress"),)
    )
    assert value._project(run(tmp_path, project_item_id="item")) == ProjectState.NOT_DONE
    value.projects = SimpleNamespace(
        list_items=lambda: (SimpleNamespace(id="item", status="Done"),)
    )
    assert value._project(run(tmp_path, project_item_id="item")) == ProjectState.DONE


def test_unproved_merge_never_becomes_merged(tmp_path: Path) -> None:
    from ai_dev_orchestrator.domain.recovery import PullRequestObservation, PullRequestState
    value = observer(tmp_path, {})
    pull = PullRequestObservation(1, "u", "owner/repo", "main", "feat/recovery", HEAD, PullRequestState.MERGED)
    value.pull_requests = SimpleNamespace(
        get_merge_snapshot=lambda _number: SimpleNamespace(
            merged=True, state="MERGED", number=1, url="u", base="main",
            head_branch="feat/recovery", head_sha=HEAD, merge_commit_sha=MERGE,
        ),
        verify_merge_commit=lambda *_: (_ for _ in ()).throw(RuntimeError("pai inválido")),
    )

    with pytest.raises(RecoveryObservationError):
        value._merge(run(tmp_path), (pull,))
