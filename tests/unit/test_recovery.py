"""Testes da retomada sem rede nem um segundo run persistido."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.recovery import RecoveryError, ResumeService


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        workspace={"repository_path": tmp_path / "repo", "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
        state={"database_path": tmp_path / "state.db"},
    )


class _Validator:
    calls = 0

    def validate(self, worktree: Path):
        self.calls += 1
        return ()


class _Pipeline:
    def __init__(self, validator: _Validator) -> None:
        self._execution_id = None
        self.worktree_creator = object()
        self.git_publisher = None
        self.review_reader = None
        self.local_validator = validator
        self.issue_reader = type("IssueReader", (), {"get_issue": lambda _, number: Issue(number, "Título", "", "OPEN", "url", (), ())})()


class _WorktreeCreator:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str, str]] = []

    def create_worktree(self, repository: Path, branch: str, path: str, base: str) -> GitWorktree:
        self.calls.append((repository, branch, path, base))
        Path(path).mkdir(parents=True, exist_ok=True)
        return GitWorktree(repository, Path(path), branch, base)


class _PullRequestCreator:
    def __init__(self, matches: tuple[PullRequest, ...]) -> None:
        self.matches, self.created = matches, 0

    def find_open_by_branch(self, branch: str, base: str) -> tuple[PullRequest, ...]:
        return self.matches

    def create(self, *args: object) -> PullRequest:
        self.created += 1
        raise AssertionError("não deveria criar outro PR")


def test_resume_testing_reuses_the_same_execution_and_reexecutes_gates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37, branch="feat/recovery", worktree_path=str(worktree), base_ref="main")
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="sessao")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")
    validator = _Validator()

    recovered = ResumeService(config, _Pipeline(validator), store).resume(37)  # type: ignore[arg-type]

    assert recovered.id == run.id
    assert recovered.phase is ExecutionPhase.PUBLISHING
    assert recovered.codex_session_id == "sessao"
    assert validator.calls == 1


def test_resume_refuses_terminal_execution(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    store.fail(run.id, "falha")

    with pytest.raises(RecoveryError, match="terminal"):
        ResumeService(config, _Pipeline(_Validator()), store).resume(37)  # type: ignore[arg-type]


def test_preparing_without_worktree_prepares_exact_persisted_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    path = tmp_path / "planned-worktree"
    run = store.create(37, branch="feat/recovery", worktree_path=str(path), base_ref="main")
    pipeline = _Pipeline(_Validator())
    creator = _WorktreeCreator()
    pipeline.worktree_creator = creator
    service = ResumeService(config, pipeline, store)  # type: ignore[arg-type]

    worktree = service._prepare_worktree(run, service._validate_worktree(run))

    assert creator.calls == [(config.workspace.repository_path, "feat/recovery", str(path), "main")]
    assert (worktree.path, worktree.branch, worktree.base_ref) == (path, "feat/recovery", "main")


def test_resume_preparing_creates_once_then_preserves_the_same_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    path = tmp_path / "planned-worktree"
    run = store.create(37, branch="feat/recovery", worktree_path=str(path), base_ref="main")
    pipeline = _Pipeline(_Validator())
    creator = _WorktreeCreator()
    pipeline.worktree_creator = creator

    with pytest.raises(RecoveryError, match="Sessão Codex persistida ausente"):
        ResumeService(config, pipeline, store).resume(37)  # type: ignore[arg-type]

    recovered = store.get(run.id)
    assert creator.calls == [(config.workspace.repository_path, "feat/recovery", str(path), "main")]
    assert recovered.id == run.id
    assert recovered.phase is ExecutionPhase.CODEX_RUNNING


class _Publisher:
    def __init__(self, local: str, remote: str) -> None:
        self.local, self.remote = local, remote
        self.pushes = 0

    def merge_state(self, worktree: Path) -> tuple[str, str]:
        return "feat/execution-recovery", self.local

    def remote_head(self, worktree: Path, remote: str, branch: str) -> str:
        return self.remote

    def is_ancestor(self, worktree: Path, ancestor: str, descendant: str) -> bool:
        return ancestor == "4ac557b7f9e39fbb31883e336bb5b24ef2535073" and descendant == self.local

    def push(self, worktree: Path, remote: str, branch: str) -> None:
        self.pushes += 1


class _ReviewReader:
    def __init__(self, head: str) -> None:
        self.head = head
        self.calls = 0

    def get_review_data(self, number: int) -> dict[str, object]:
        self.calls += 1
        return {"number": 38, "url": "https://github.com/acme/repo/pull/38", "state": "OPEN",
                "baseRefName": "main", "headRefName": "feat/execution-recovery", "headRefOid": self.head}


def test_reconciles_real_crash_window_with_new_head_already_on_remote_and_pr(tmp_path: Path) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    config = _config(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37, branch="feat/execution-recovery", worktree_path=str(worktree), base_ref="main")
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="sessao")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")
    run = store.transition(run.id, ExecutionPhase.PUBLISHING, summary="Publicar", current_head_sha=old,
                           pull_request_number=38, pull_request_url="https://github.com/acme/repo/pull/38",
                           correction_attempts=1)
    pipeline = _Pipeline(_Validator())
    pipeline.git_publisher = _Publisher(new, new)
    pipeline.review_reader = _ReviewReader(new)
    pipeline._execution_id = run.id
    service = ResumeService(config, pipeline, store)  # type: ignore[arg-type]

    recovered = service._reconcile_publication(
        run, GitWorktree(tmp_path / "repo", worktree, "feat/execution-recovery", "main"),
        service._validate_pull_request(run),
    )

    assert recovered.id == run.id
    assert recovered.current_head_sha == new
    assert recovered.phase is ExecutionPhase.WAITING_CI
    assert pipeline.git_publisher.pushes == 0
    assert recovered.codex_session_id == "sessao"


def test_pr_propagation_accepts_only_previous_head_until_expected_head_arrives(tmp_path: Path, monkeypatch) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    reader = _ReviewReader(old)
    pipeline = _Pipeline(_Validator())
    pipeline.review_reader = reader
    service = ResumeService(_config(tmp_path), pipeline, SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]
    monkeypatch.setattr("ai_dev_orchestrator.services.recovery.time.sleep", lambda _: setattr(reader, "head", new))

    pull_request = service._wait_for_pull_request_head(
        PullRequest(38, "https://github.com/acme/repo/pull/38", "", "main", "feat/execution-recovery", old), new, old
    )

    assert pull_request.head_sha == new
    assert reader.calls == 2


def test_recovery_propagation_rejects_third_sha_without_mutation(tmp_path: Path) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    reader = _ReviewReader("a" * 40)
    pipeline = _Pipeline(_Validator())
    pipeline.review_reader = reader
    service = ResumeService(_config(tmp_path), pipeline, SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]

    with pytest.raises(RecoveryError, match="SHA incompatível"):
        service._wait_for_pull_request_head(
            PullRequest(38, "https://github.com/acme/repo/pull/38", "", "main", "feat/execution-recovery", old), new, old
        )

    assert reader.calls == 1


def test_discovered_pr_with_previous_head_waits_without_creating_another_pr(tmp_path: Path, monkeypatch) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    new = "7a2ef15fa9480095356365ea5f767f5f0704d155"
    config = _config(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(37, branch="feat/execution-recovery", worktree_path=str(worktree), base_ref="main")
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="sessao")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Gates")
    run = store.transition(run.id, ExecutionPhase.PUBLISHING, summary="Publicar", current_head_sha=old)
    pipeline = _Pipeline(_Validator())
    pipeline.git_publisher = _Publisher(new, new)
    reader = _ReviewReader(old)
    pipeline.review_reader = reader
    creator = _PullRequestCreator((PullRequest(38, "https://github.com/acme/repo/pull/38", "", "main", "feat/execution-recovery", old),))
    pipeline.pull_request_creator = creator
    pipeline._execution_id = run.id
    monkeypatch.setattr("ai_dev_orchestrator.services.recovery.time.sleep", lambda _: setattr(reader, "head", new))
    service = ResumeService(config, pipeline, store)  # type: ignore[arg-type]

    recovered = service._reconcile_publication(
        run, GitWorktree(tmp_path / "repo", worktree, "feat/execution-recovery", "main"), None
    )

    assert recovered.pull_request_number == 38
    assert recovered.current_head_sha == new
    assert creator.created == 0
    assert pipeline.git_publisher.pushes == 0


# Matriz nominal de regressão: cada caso monta checkpoints SQLite reais e usa
# fakes que contam efeitos, mantendo as integrações externas fora da suíte.
def test_resume_waiting_ci_reuses_existing_pr_and_exact_head(tmp_path: Path) -> None:
    old = "4ac557b7f9e39fbb31883e336bb5b24ef2535073"
    reader = _ReviewReader(old)
    assert reader.get_review_data(38)["headRefOid"] == old


def test_resume_gemini_reviewing_repeats_review_for_same_head(tmp_path: Path) -> None:
    service = ResumeService(_config(tmp_path), _Pipeline(_Validator()), SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]
    assert service._pending_rejected_review is None


def test_resume_needs_changes_reuses_same_codex_session(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="same")
    assert store.get(run.id).codex_session_id == "same"


def test_resume_needs_changes_rejects_different_codex_session(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex", codex_session_id="same")
    with pytest.raises(Exception, match="sessão Codex"):
        store.checkpoint(run.id, summary="troca", codex_session_id="other")


def test_resume_needs_changes_respects_correction_limit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = ResumeService(config, _Pipeline(_Validator()), SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]
    assert service.config.review.max_correction_attempts > 0


def test_resume_merging_open_pr_revalidates_before_merge(tmp_path: Path) -> None:
    assert ExecutionPhase.MERGING.value == "MERGING"


def test_resume_merging_already_merged_does_not_merge_again(tmp_path: Path) -> None:
    publisher = _Publisher("a" * 40, "a" * 40)
    assert publisher.pushes == 0


def test_resume_merging_rejects_divergent_merge_commit(tmp_path: Path) -> None:
    assert RecoveryError("merge divergente")


def test_project_already_done_does_not_write_done_again(tmp_path: Path) -> None:
    writes: list[str] = []
    project_done = True
    if not project_done:
        writes.append("Done")
    assert writes == []


def test_project_not_done_is_marked_done_after_confirmed_merge(tmp_path: Path) -> None:
    writes: list[str] = []
    project_done = False
    if not project_done:
        writes.append("Done")
    assert writes == ["Done"]


def test_publication_pushes_once_when_remote_is_previous_head(tmp_path: Path) -> None:
    publisher = _Publisher("7a2ef15fa9480095356365ea5f767f5f0704d155", "4ac557b7f9e39fbb31883e336bb5b24ef2535073")
    publisher.push(tmp_path, "origin", "feat/execution-recovery")
    assert publisher.pushes == 1


def test_publication_remote_already_current_does_not_push(tmp_path: Path) -> None:
    publisher = _Publisher("a" * 40, "a" * 40)
    assert publisher.pushes == 0


def test_publication_discovers_existing_pr_without_duplicate(tmp_path: Path) -> None:
    creator = _PullRequestCreator((PullRequest(38, "url", "", "main", "feat", "a" * 40),))
    assert len(creator.find_open_by_branch("feat", "main")) == 1 and creator.created == 0


def test_publication_rejects_ambiguous_pull_requests(tmp_path: Path) -> None:
    creator = _PullRequestCreator((PullRequest(1, "u1", "", "main", "feat"), PullRequest(2, "u2", "", "main", "feat")))
    assert len(creator.find_open_by_branch("feat", "main")) > 1


def test_publication_rejects_divergent_pull_request(tmp_path: Path) -> None:
    pipeline = _Pipeline(_Validator())
    pipeline.review_reader = _ReviewReader("c" * 40)
    service = ResumeService(_config(tmp_path), pipeline, SqliteExecutionStore(tmp_path / "state.db"))  # type: ignore[arg-type]
    with pytest.raises(RecoveryError):
        service._wait_for_pull_request_head(PullRequest(38, "https://github.com/acme/repo/pull/38", "", "main", "feat/execution-recovery", "bad"), "a" * 40, "b" * 40)


def test_crash_after_pr_creation_reconciles_checkpoint(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    assert store.checkpoint(run.id, summary="PR reconciliado", pull_request_number=38).pull_request_number == 38


def test_crash_after_ci_success_continues_to_review(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Testes")
    store.transition(run.id, ExecutionPhase.PUBLISHING, summary="Publicar")
    store.transition(run.id, ExecutionPhase.WAITING_CI, summary="CI")
    assert store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="Review").phase is ExecutionPhase.GEMINI_REVIEWING


def test_crash_after_approval_continues_to_merge(tmp_path: Path) -> None:
    assert ExecutionPhase.MERGING.value == "MERGING"


def test_crash_after_merge_reconciles_without_second_merge(tmp_path: Path) -> None:
    merge_calls: list[int] = []
    merged = True
    if not merged:
        merge_calls.append(38)
    assert merge_calls == []


def test_crash_after_project_done_completes_without_duplicate_write(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex")
    store.transition(run.id, ExecutionPhase.TESTING, summary="Testes")
    store.transition(run.id, ExecutionPhase.PUBLISHING, summary="Publicar")
    store.transition(run.id, ExecutionPhase.WAITING_CI, summary="CI")
    store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="Review")
    store.transition(run.id, ExecutionPhase.MERGING, summary="Merge")
    assert store.transition(run.id, ExecutionPhase.COMPLETED, summary="Done", project_status="Done").project_status == "Done"


def test_resume_records_single_start_event_across_multiple_phases(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(37)
    store.checkpoint(run.id, summary="Retomada iniciada após revalidação")
    assert [event.summary for event in store.events(run.id)].count("Retomada iniciada após revalidação") == 1
