"""Cobertura da configuração por projeto e das esperas de provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_orchestrator.adapters.codex import CodexAdapter
from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.config import OrchestratorConfig, load_config
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.provider import (
    ProviderFailureKind,
    classify_provider_text,
)
from ai_dev_orchestrator.infrastructure.database import (
    ExecutionStoreError,
    SqliteExecutionStore,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.init_project import (
    ProjectDiscovery,
    ProjectInitService,
)
from ai_dev_orchestrator.services.resume import ResumeService
from ai_dev_orchestrator.services.resume import ResumeError
from ai_dev_orchestrator.services.supervisor import SupervisorService


def _config(tmp_path: Path, **providers: str) -> OrchestratorConfig:
    return OrchestratorConfig(
        github={
            "owner": "acme",
            "repository": "repo",
            "project_number": 1,
            "ready_status": "Ready",
            "pull_request_target": "develop",
            "protected_branches": ("main",),
        },
        workspace={
            "repository_path": tmp_path / "repo",
            "worktrees_dir": tmp_path / "worktrees",
            "base_branch": "develop",
        },
        providers=providers,
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        state={"database_path": tmp_path / "state.db"},
    )


@dataclass
class Runner:
    result: CommandResult
    calls: list[list[str]] = field(default_factory=list)

    def run(self, arguments, input_text=None):
        self.calls.append(list(arguments))
        return self.result


def _jsonl() -> str:
    return "\n".join(
        (
            '{"type":"thread.started","thread_id":"session"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            '{"type":"turn.completed"}',
        )
    )


def test_modelo_codex_explicito_e_encaminhado_em_execute_e_resume(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = Runner(CommandResult(0, _jsonl()))
    adapter = CodexAdapter(runner, model="gpt-explicito")

    adapter.execute(worktree, "implemente")
    adapter.resume(worktree, "session", "corrija")

    assert all(
        call[call.index("--model") + 1] == "gpt-explicito" for call in runner.calls
    )


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("HTTP 429: rate limit", ProviderFailureKind.TRANSIENT_RATE_LIMIT),
        ("authentication failed", ProviderFailureKind.AUTH_ERROR),
        ("model not found", ProviderFailureKind.MODEL_UNAVAILABLE),
        ("texto inespecífico", ProviderFailureKind.UNKNOWN),
    ],
)
def test_classificacao_conservadora(message: str, kind: ProviderFailureKind) -> None:
    assert classify_provider_text(message) is kind


def test_quota_preserva_identidade_e_modelos_no_mesmo_run(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(
        44,
        branch="work/quota",
        worktree_path="C:/worktree",
        base_ref="develop",
        codex_model="modelo-a",
        gemini_model="modelo-b",
    )
    run = store.transition(
        run.id,
        ExecutionPhase.CODEX_RUNNING,
        summary="início",
        codex_session_id="session-44",
    )
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    waiting = store.transition(
        run.id,
        ExecutionPhase.WAITING_CODEX_QUOTA,
        summary="quota",
        quota_provider="codex",
        quota_classification="TRANSIENT_RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=retry_at.isoformat(),
    )

    assert waiting.id == run.id
    assert waiting.codex_session_id == "session-44"
    assert waiting.branch == "work/quota" and waiting.worktree_path == "C:/worktree"
    assert waiting.quota_retry_at == retry_at
    with pytest.raises(ExecutionStoreError, match="modelo"):
        store.checkpoint(run.id, summary="troca", codex_model="modelo-outro")


def test_resume_antes_do_retry_nao_chama_observer(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(
        44, branch="work/quota", worktree_path=str(tmp_path / "wt"), base_ref="develop"
    )
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="início")
    store.transition(
        run.id,
        ExecutionPhase.WAITING_CODEX_QUOTA,
        summary="quota",
        quota_provider="codex",
        quota_classification="TRANSIENT_RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    class Never:
        def __getattr__(self, _name):
            raise AssertionError("efeito não deveria ser chamado")

    result = ResumeService(store, Never(), Never(), Never()).resume(44)

    assert result.execution_id == run.id
    assert result.phase == "WAITING_CODEX_QUOTA"
    assert result.quota_retry_at is not None


def test_quota_sem_reset_nao_inventa_horario(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(
        44, branch="work/quota", worktree_path=str(tmp_path / "wt"), base_ref="develop"
    )
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="início")
    store.transition(
        run.id,
        ExecutionPhase.WAITING_CODEX_QUOTA,
        summary="quota",
        quota_provider="codex",
        quota_classification="TERMINAL_QUOTA",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=None,
    )

    with pytest.raises(ResumeError, match="não informou"):
        ResumeService(store, object(), object(), object()).resume(44)
    assert store.get(run.id).quota_retry_at is None


def test_supervisor_aguarda_sem_chamar_work_antes_do_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(
        44, branch="work/quota", worktree_path=str(tmp_path / "wt"), base_ref="develop"
    )
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="início")
    store.transition(
        run.id,
        ExecutionPhase.WAITING_CODEX_QUOTA,
        summary="quota",
        quota_provider="codex",
        quota_classification="TRANSIENT_RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    class Work:
        def work(self):
            raise AssertionError("work não deve rodar antes do retry")

    sleeps: list[float] = []

    def interrupt(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config, Work(), store, interrupt).watch()

    assert sleeps == [config.supervisor.max_sleep_seconds]
    assert not config.state.database_path.with_suffix(".watch.lock").exists()


def test_render_atomico_usa_campos_novos_e_recarrega(tmp_path: Path) -> None:
    config = _config(tmp_path, codex_model="codex-x", gemini_model="gemini-y")
    path = tmp_path / "orchestrator.toml"

    ProjectInitService().write(path, config)
    loaded = load_config(path)

    assert loaded.workspace.base_branch == "develop"
    assert loaded.github.pull_request_target == "develop"
    assert loaded.github.protected_branches == ("main",)
    assert loaded.providers.codex_model == "codex-x"
    assert not tuple(tmp_path.glob(".orchestrator.toml.*.tmp"))


def test_cancelamento_do_init_nao_grava_arquivo(tmp_path: Path, monkeypatch) -> None:
    discovery = ProjectDiscovery(
        tmp_path,
        "origin",
        "https://github.com/acme/repo.git",
        "acme",
        "repo",
        "main",
        ("main",),
        "main",
        (),
        (1,),
    )
    monkeypatch.setattr(ProjectInitService, "discover", lambda self, cwd: discovery)
    result = CliRunner().invoke(app, ["init"], input="\n\n\n\n\nN\n")

    assert result.exit_code != 0
    assert not (tmp_path / "orchestrator.toml").exists()
