"""Contratos dos efeitos reais com adapters substituídos por fakes locais."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.ci import CiResult, CiStatus, StatusCheck
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.review import FindingSeverity, ReviewFinding, ReviewVerdict, StructuredReview
from ai_dev_orchestrator.services.merge import MergePullRequestSnapshot, MergeResult
from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
from ai_dev_orchestrator.services.convergence import ConvergencePoller

HEAD = "a" * 40
MERGE = "b" * 40
URL = "https://github.com/owner/repo/pull/37"


def config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "owner", "repository": "repo", "project_number": 1,
                "ready_status": "Ready", "pull_request_base": "main"},
        workspace={"repository_path": tmp_path / "repo",
                   "worktrees_dir": tmp_path / "worktrees", "base_ref": "origin/main"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": True},
    )


def run(tmp_path: Path, **changes: object) -> RunRecord:
    now = datetime(2026, 1, 1)
    values: dict[str, object] = {
        "id": "execution", "issue_number": 37, "phase": ExecutionPhase.GEMINI_REVIEWING,
        "created_at": now, "updated_at": now, "branch": "feat/recovery",
        "worktree_path": str(tmp_path / "worktree"), "base_ref": "origin/main",
        "codex_session_id": "session", "pull_request_number": 37,
        "pull_request_url": URL, "current_head_sha": HEAD, "reviewed_head_sha": HEAD,
    }
    values.update(changes)
    return RunRecord(**values)  # type: ignore[arg-type]


def effects(tmp_path: Path) -> RecoveryEffects:
    value = object.__new__(RecoveryEffects)
    value.config = config(tmp_path)
    return value


def issue() -> Issue:
    return Issue(37, "Retomada", "Corpo", "OPEN", "url", (), ())


def test_start_and_resume_codex_keep_the_expected_session(tmp_path: Path) -> None:
    value = effects(tmp_path)
    calls: list[tuple[str, str]] = []
    value.issues = SimpleNamespace(get_issue=lambda _number: issue())
    value.codex = SimpleNamespace(
        execute=lambda _path, prompt: (calls.append(("execute", prompt)) or SimpleNamespace(session_id="session")),
        resume=lambda _path, session, prompt: (calls.append((session, prompt)) or SimpleNamespace(session_id=session)),
    )

    assert value.start_codex(run(tmp_path, codex_session_id=None)) == "session"
    assert value.resume_codex(run(tmp_path)) == "session"
    assert calls[0][0] == "execute"
    assert calls[1][0] == "session"


def test_commit_selects_initial_or_correction_operation(tmp_path: Path) -> None:
    value = effects(tmp_path)
    calls: list[str] = []
    value.publication = SimpleNamespace(
        current_head=lambda _path: HEAD,
        commit=lambda _path, _issue: (calls.append("initial") or MERGE),
        commit_correction=lambda _path: (calls.append("correction") or MERGE),
    )

    assert value.create_commit(run(tmp_path, pull_request_number=None)).new_head_sha == MERGE
    assert value.create_commit(run(tmp_path)).new_head_sha == MERGE
    assert calls == ["initial", "correction"]


def test_create_pull_request_rereads_remote_identity(tmp_path: Path) -> None:
    value = effects(tmp_path)
    value.issues = SimpleNamespace(get_issue=lambda _number: issue())
    value.validation = SimpleNamespace(validate=lambda _path: ())
    value.pull_requests = SimpleNamespace(
        create=lambda *_: PullRequest(37, "url-criada", "t", "main", "feat/recovery"),
        get_merge_snapshot=lambda _number: MergePullRequestSnapshot(
            37, URL, "OPEN", False, "main", "feat/recovery", HEAD, "MERGEABLE"
        ),
    )

    observed = value.create_pull_request(run(tmp_path))

    assert observed.url == URL
    assert observed.head_sha == HEAD


def test_push_waits_for_pr_head_without_repeating_remote_mutation(tmp_path: Path) -> None:
    value = effects(tmp_path)
    old = "c" * 40
    reads: list[str] = []
    pushes: list[str] = []
    snapshots = iter((old, old, HEAD))
    value.convergence = ConvergencePoller(
        value.config.convergence, monotonic=lambda: 0, sleep=lambda _seconds: None
    )
    value.publication = SimpleNamespace(
        push=lambda _path, _remote, _branch: pushes.append("push")
    )

    def snapshot(_number: int) -> MergePullRequestSnapshot:
        head = next(snapshots)
        reads.append(head)
        return MergePullRequestSnapshot(
            37, URL, "OPEN", False, "main", "feat/recovery", head, "MERGEABLE"
        )

    value.pull_requests = SimpleNamespace(get_merge_snapshot=snapshot)

    value.push_branch(run(tmp_path))

    assert pushes == ["push"]
    assert reads == [old, old, HEAD]


def test_wait_for_ci_preserves_real_status_and_head(tmp_path: Path) -> None:
    value = effects(tmp_path)
    check = StatusCheck("test", "COMPLETED", "SUCCESS")
    value._wait_ci_result = lambda _run: CiResult(HEAD, (check,), CiStatus.SUCCESS)  # type: ignore[method-assign]

    observed = value.wait_for_ci(run(tmp_path))

    assert observed.head_sha == HEAD
    assert observed.state.value == "SUCCESS"


def test_review_receives_real_ci_checks_and_prior_findings(tmp_path: Path, monkeypatch) -> None:
    value = effects(tmp_path)
    prior = (ReviewFinding(FindingSeverity.LOW, "Anterior", "Descrição"),)
    check = StatusCheck("test", "COMPLETED", "SUCCESS")
    ci = CiResult(HEAD, (check,), CiStatus.SUCCESS)
    captured: list[tuple[CiResult, tuple[ReviewFinding, ...]]] = []
    expected = StructuredReview(ReviewVerdict.APPROVED, (), HEAD, "ok")
    value.issues = SimpleNamespace(get_issue=lambda _number: issue())
    value.validation = SimpleNamespace(validate=lambda _path: ())
    value._wait_ci_result = lambda _run: ci  # type: ignore[method-assign]
    value.projects = value.worktrees = value.codex = value.publication = object()
    value.pull_requests = value.reviewer = object()

    def fake_review(_pipeline, _issue, _worktree, _pull, _head, _gates,
                    ci_result, prior_findings):
        captured.append((ci_result, prior_findings))
        return expected

    monkeypatch.setattr("ai_dev_orchestrator.services.pipeline.RunPipeline._review_head", fake_review)

    assert value.review_head(run(tmp_path), prior) == expected
    assert captured == [(ci, prior)]
    assert captured[0][0].checks == (check,)


def test_resume_correction_sends_all_persisted_finding_fields_to_same_session(tmp_path: Path) -> None:
    value = effects(tmp_path)
    finding = ReviewFinding(FindingSeverity.HIGH, "Título", "Descrição", "src/a.py", 12,
                            "Critério 8")
    prompts: list[tuple[str, str]] = []
    value.issues = SimpleNamespace(get_issue=lambda _number: issue())
    value.codex = SimpleNamespace(
        resume=lambda _path, session, prompt: (prompts.append((session, prompt)) or SimpleNamespace(session_id=session))
    )

    assert value.resume_correction(run(tmp_path), (finding,)) == "session"
    session, prompt = prompts[0]
    assert session == "session"
    for text in ("Título", "Descrição", "src/a.py", "12", "Critério 8"):
        assert text in prompt


def test_merge_gates_before_mutation_and_verifies_afterward(tmp_path: Path) -> None:
    value = effects(tmp_path)
    order: list[str] = []
    open_snapshot = MergePullRequestSnapshot(
        37, URL, "OPEN", False, "main", "feat/recovery", HEAD, "MERGEABLE"
    )
    merged_snapshot = MergePullRequestSnapshot(
        37, URL, "MERGED", False, "main", "feat/recovery", HEAD, "UNKNOWN",
        True, MERGE,
    )
    snapshots = iter((open_snapshot, merged_snapshot))
    value.publication = SimpleNamespace(merge_state=lambda _path: ("feat/recovery", HEAD))
    value._wait_ci_result = lambda _run: (order.append("ci") or CiResult(HEAD, (), CiStatus.SUCCESS))  # type: ignore[method-assign]
    value.pull_requests = SimpleNamespace(
        get_merge_snapshot=lambda _number: (order.append("snapshot") or next(snapshots)),
        merge=lambda _number, _head: (order.append("merge") or MergeResult(HEAD, MERGE)),
        verify_merge_commit=lambda merge, head: order.append(f"verify:{merge}:{head}"),
    )

    result = value.merge_pull_request(run(tmp_path, review_verdict="APPROVED"))

    assert result.merge_commit_sha == MERGE
    assert order == ["snapshot", "ci", "merge", "snapshot", f"verify:{MERGE}:{HEAD}"]


def test_mark_project_done_writes_once(tmp_path: Path) -> None:
    value = effects(tmp_path)
    calls: list[tuple[str, str]] = []
    value.projects = SimpleNamespace(set_status=lambda item, status: calls.append((item, status)))

    value.mark_project_done(run(tmp_path, project_item_id="item"))

    assert calls == [("item", "Done")]
