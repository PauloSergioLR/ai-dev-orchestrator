"""Recovery de contrato usa Git real e ignora o worktree corrente dirty."""

from pathlib import Path
import subprocess

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.contract_recovery import ContractRecoveryService


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_recovers_poisoned_contract_from_exact_base_not_dirty_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git("init", "-b", "main", cwd=repository)
    git("config", "user.name", "Teste", cwd=repository)
    git("config", "user.email", "teste@example.invalid", cwd=repository)
    workflow = repository / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  quality:\n    steps:\n      - name: Test\n        run: ./validate\n",
        encoding="utf-8",
    )
    (repository / "validate").write_text("baseline", encoding="utf-8")
    git("add", ".", cwd=repository)
    git("commit", "-m", "base", cwd=repository)
    base_sha = git("rev-parse", "HEAD", cwd=repository)

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "pyproject.toml").write_text(
        "[tool.poison]\ncommand='deploy --production'\n", encoding="utf-8"
    )
    config = OrchestratorConfig(
        github={
            "owner": "acme", "repository": "repo", "project_number": 1,
            "ready_status": "Ready",
        },
        workspace={
            "repository_path": repository,
            "worktrees_dir": tmp_path / "worktrees",
            "base_branch": "main",
        },
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(
        91,
        branch="work/recover-contract",
        worktree_path=str(dirty),
        base_ref="main",
        base_sha=base_sha,
        repository_identity="acme/repo",
        contract_fingerprint="poisoned",
        project_contract_json="{}",
    )
    service = ContractRecoveryService(config, store, GitWorktreeAdapter())

    preview = service.preview(91)
    recovered = service.recover(
        91, expected_fingerprint=preview.recovered.fingerprint
    )

    assert recovered.id == run.id
    assert recovered.contract_fingerprint == preview.recovered.fingerprint
    assert recovered.contract_fingerprint != "poisoned"
    assert [gate.argv for gate in preview.recovered.gates] == [("./validate",)]
    assert not config.workspace.worktrees_dir.exists() or not any(
        config.workspace.worktrees_dir.iterdir()
    )
    assert (dirty / "pyproject.toml").exists()
