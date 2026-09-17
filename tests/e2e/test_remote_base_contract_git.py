"""Novo run sincroniza a base remota real antes de congelar o contrato."""

from pathlib import Path
import subprocess

from ai_dev_orchestrator.adapters.codex import CodexExecution
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.domain.project_contract import ProjectContract
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipeline
from ai_dev_orchestrator.services.work import WorkService


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


class World:
    def list_items(self):
        return (
            ProjectItem(
                "item-92", "Issue", 92, "Atualizar contrato remoto", "url",
                "acme/repo", "Ready", None, None, None, None,
            ),
        )

    def get_issue(self, number: int):
        return Issue(number, "Atualizar contrato remoto", "", "OPEN", "url", (), ())

    def set_status(self, _item: str, _status: str) -> None:
        pass


class Codex:
    def execute(self, _worktree, _prompt):
        return CodexExecution("session-92", "ok", "", "", True)

    def resume(self, _worktree, session_id, _prompt):
        return CodexExecution(session_id, "ok", "", "", True)


class NeverResume:
    def resume(self, _issue):
        raise AssertionError("não existe run anterior")


def test_remote_commit_is_both_worktree_origin_and_frozen_contract(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", str(remote), str(author)], check=True, capture_output=True)
    git("switch", "-c", "main", cwd=author)
    git("config", "user.name", "Teste", cwd=author)
    git("config", "user.email", "teste@example.invalid", cwd=author)
    workflow = author / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  quality:\n    steps:\n      - name: Test\n        run: ./validate-a\n",
        encoding="utf-8",
    )
    (author / "validate-a").write_text("A", encoding="utf-8")
    git("add", ".", cwd=author)
    git("commit", "-m", "A", cwd=author)
    git("push", "-u", "origin", "main", cwd=author)

    repository = tmp_path / "repository"
    subprocess.run(["git", "clone", str(remote), str(repository)], check=True, capture_output=True)
    git("switch", "-c", "main", "origin/main", cwd=repository)
    stale_sha = git("rev-parse", "HEAD", cwd=repository)

    workflow.write_text(
        "jobs:\n  quality:\n    steps:\n      - name: Test\n        run: ./validate-b\n",
        encoding="utf-8",
    )
    (author / "validate-b").write_text("B", encoding="utf-8")
    git("add", ".", cwd=author)
    git("commit", "-m", "B", cwd=author)
    git("push", "origin", "main", cwd=author)
    remote_sha = git("rev-parse", "HEAD", cwd=author)
    assert stale_sha != remote_sha

    config = OrchestratorConfig(
        github={
            "owner": "acme", "repository": "repo", "project_number": 1,
            "ready_status": "Ready",
        },
        workspace={
            "repository_path": repository,
            "worktrees_dir": tmp_path / "worktrees",
            "base_branch": "main",
            "remote_name": "origin",
        },
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )
    store = SqliteExecutionStore(config.state.database_path)
    world = World()
    pipeline = RunPipeline(
        config,
        world,
        world,
        world,
        GitWorktreeAdapter(),
        Codex(),
        execution_store=store,
        resolve_contract_at_start=True,
    )
    service = WorkService(
        config,
        store,
        world,
        world,
        pipeline,
        NeverResume(),
        GitWorktreeAdapter(),
        world,
    )

    result = service.start_next()

    assert result is not None and result.run is not None
    persisted = store.get_latest_for_issue(92)
    assert persisted is not None
    assert persisted.base_sha == result.run.base_sha == remote_sha
    assert git("rev-parse", "HEAD", cwd=result.run.worktree_path) == remote_sha
    contract = ProjectContract.from_json(persisted.project_contract_json or "")
    assert [gate.argv for gate in contract.gates] == [("./validate-b",)]
