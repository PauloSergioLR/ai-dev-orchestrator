"""Testes da camada autônoma de seleção e retomada."""

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.services.pipeline import RunResult
from ai_dev_orchestrator.services.resume import ResumeResult
from ai_dev_orchestrator.services.work import WorkError, WorkService, branch_from_title


def config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={
            "owner": "acme",
            "repository": "repo",
            "project_number": 1,
            "ready_status": "Ready",
        },
        workspace={
            "repository_path": tmp_path / "repo",
            "worktrees_dir": tmp_path / "worktrees",
            "base_ref": "main",
            "remote_name": "origin",
        },
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )


def item(
    number: int,
    *,
    priority: str | None = None,
    status: str = "Ready",
    repository: str = "acme/repo",
    content_type: str = "Issue",
    agent: str | None = None,
) -> ProjectItem:
    return ProjectItem(
        f"item-{number}", content_type, number if content_type == "Issue" else None,
        f"Título {number}", "url", repository, status, priority, None, None, agent,
    )


@dataclass
class Store:
    active: tuple[object, ...] = ()

    def list_active(self) -> tuple[object, ...]:
        return self.active


@dataclass
class Projects:
    items: tuple[ProjectItem, ...]
    calls: int = 0

    def list_items(self) -> tuple[ProjectItem, ...]:
        self.calls += 1
        return self.items


@dataclass
class Issues:
    states: dict[int, str] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)

    def get_issue(self, number: int) -> Issue:
        self.calls.append(number)
        return Issue(number, f"Implementar ação número {number}!", "", self.states.get(number, "OPEN"), "url", (), ())


@dataclass
class Pipeline:
    calls: list[tuple[int, str, str | None]] = field(default_factory=list)

    def run(self, issue_number: int, branch: str, *, base_ref: str | None = None) -> RunResult:
        self.calls.append((issue_number, branch, base_ref))
        return RunResult(issue_number, f"item-{issue_number}", branch, Path("worktree"), base_ref or "", "session", "fim", "Done")


@dataclass
class Resumer:
    calls: list[int] = field(default_factory=list)

    def resume(self, issue_number: int) -> ResumeResult:
        self.calls.append(issue_number)
        return ResumeResult(issue_number, "execution", "WAITING_CI", "work/x", "session", 9, "a" * 40, 1)


@dataclass
class Synchronizer:
    calls: list[tuple[object, str, str, str]] = field(default_factory=list)
    error: Exception | None = None

    def prepare_remote_base(self, repository: object, remote: str, base: str, branch: str) -> str:
        self.calls.append((repository, remote, base, branch))
        if self.error:
            raise self.error
        return "refs/remotes/origin/main"


def service(tmp_path: Path, items: tuple[ProjectItem, ...], *, active: tuple[object, ...] = ()) -> tuple[WorkService, Projects, Issues, Pipeline, Resumer, Synchronizer]:
    projects, issues = Projects(items), Issues()
    pipeline, resumer, sync = Pipeline(), Resumer(), Synchronizer()
    return WorkService(config(tmp_path), Store(active), projects, issues, pipeline, resumer, sync), projects, issues, pipeline, resumer, sync


def test_selects_ready_issue_syncs_base_and_reuses_pipeline(tmp_path: Path) -> None:
    work, _, _, pipeline, _, sync = service(tmp_path, (item(7),))

    result = work.work()

    assert result is not None and result.run is not None and not result.resumed
    assert pipeline.calls == [(7, "work/implementar-acao-numero", "refs/remotes/origin/main")]
    assert sync.calls[0][1:3] == ("origin", "main")


def test_priority_and_issue_number_are_deterministic(tmp_path: Path) -> None:
    work, _, issues, pipeline, _, _ = service(
        tmp_path,
        (item(2, priority="P2"), item(9, priority="P0"), item(4, priority="P1"),
         item(3, priority="P0"), item(1, priority="P3"), item(8)),
    )

    work.work()

    assert issues.calls == [3]
    assert pipeline.calls[0][0] == 3


def test_ignores_ineligible_items_closed_issues_and_incompatible_agent(tmp_path: Path) -> None:
    work, _, issues, pipeline, _, _ = service(
        tmp_path,
        (item(1, repository="other/repo"), item(2, status="Backlog"),
         item(3, content_type="DraftIssue"), item(4, agent="Gemini"),
         item(5, priority="P0"), item(6, priority="P1", agent="codex")),
    )
    issues.states[5] = "CLOSED"

    work.work()

    assert issues.calls == [5, 6]
    assert pipeline.calls[0][0] == 6


def test_no_eligible_issue_finishes_without_effects(tmp_path: Path) -> None:
    work, _, _, pipeline, _, sync = service(tmp_path, (item(1, status="Done"),))

    assert work.work() is None
    assert pipeline.calls == sync.calls == []


def test_active_execution_resumes_without_reading_project(tmp_path: Path) -> None:
    active = (SimpleNamespace(issue_number=42),)
    work, projects, _, pipeline, resumer, sync = service(tmp_path, (item(1),), active=active)

    result = work.work()

    assert result is not None and result.resumed
    assert resumer.calls == [42]
    assert projects.calls == 0
    assert pipeline.calls == sync.calls == []


def test_multiple_active_executions_fail_closed(tmp_path: Path) -> None:
    active = (SimpleNamespace(issue_number=1), SimpleNamespace(issue_number=2))
    work, projects, _, pipeline, resumer, _ = service(tmp_path, (), active=active)

    with pytest.raises(WorkError, match="ambíguas"):
        work.work()

    assert projects.calls == 0
    assert pipeline.calls == resumer.calls == []


def test_sync_failure_does_not_start_pipeline(tmp_path: Path) -> None:
    work, _, _, pipeline, _, sync = service(tmp_path, (item(7),))
    sync.error = RuntimeError("fetch recusado")

    with pytest.raises(WorkError, match="sincronizar.*fetch recusado"):
        work.work()

    assert pipeline.calls == []


@pytest.mark.parametrize(
    "title, expected",
    [("Correção: API #42", "work/correcao-api"), ("!!!", "work/trabalho")],
)
def test_branch_slug_is_safe_ascii_and_does_not_add_issue_number(title: str, expected: str) -> None:
    assert branch_from_title(title, 42) == expected
