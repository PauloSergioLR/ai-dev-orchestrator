"""Cobertura da configuração por projeto e das esperas de provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.adapters.codex import CodexAdapter
from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.config import OrchestratorConfig, load_config
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.recovery import (
    CiObservation,
    CiState,
    PullRequestObservation,
    PullRequestState,
    RecoveryObservation,
    RecoveryPolicy,
    WorktreeState,
)
from ai_dev_orchestrator.domain.provider import (
    ProviderFailure,
    ProviderFailureKind,
    classify_provider_text,
)
from ai_dev_orchestrator.domain.review import ReviewVerdict, StructuredReview
from ai_dev_orchestrator.infrastructure.database import (
    ExecutionStoreError,
    SqliteExecutionStore,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.init_project import (
    ProjectDiscovery,
    ProjectInitService,
)
from ai_dev_orchestrator.services.pipeline import RunPipeline, RunPipelineError
from ai_dev_orchestrator.services.recovery_executor import RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
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


@dataclass
class CwdRunner:
    results: dict[tuple[str, ...], CommandResult]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, arguments, cwd=None, input_text=None):
        key = tuple(arguments)
        self.calls.append(key)
        return self.results.get(key, CommandResult(1, stderr="indisponível"))


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


def test_default_e_auto_nao_passam_flag_de_modelo(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    codex_runner = Runner(CommandResult(0, _jsonl()))
    CodexAdapter(codex_runner, model="default").execute(worktree, "implemente")
    assert "--model" not in codex_runner.calls[0]

    envelope = '{"status":"SUCCESS","structured_output":{"ok":true}}'

    class SingleResult:
        def __init__(self):
            self.calls = []

        def run(self, arguments, cwd=None, input_text=None):
            self.calls.append(list(arguments))
            return CommandResult(0, envelope)

    agy = SingleResult()
    AntigravityAdapter(10, agy, model="default").invoke("revise", worktree, {})
    assert "--model" not in agy.calls[0]
    assert (
        OrchestratorConfig(
            github={
                "owner": "a",
                "repository": "b",
                "project_number": 1,
                "ready_status": "Ready",
            },
            workspace={
                "repository_path": tmp_path,
                "worktrees_dir": tmp_path,
                "base_branch": "main",
            },
            execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
            providers={"codex_model": "auto", "gemini_model": "auto"},
        ).providers.codex_model
        == "default"
    )


def test_modelo_explicito_e_encaminhado_ao_antigravity(tmp_path: Path) -> None:
    class AgyRunner:
        def __init__(self):
            self.arguments = []

        def run(self, arguments, cwd=None, input_text=None):
            self.arguments = list(arguments)
            return CommandResult(0, '{"status":"SUCCESS","structured_output":{}}')

    runner = AgyRunner()
    AntigravityAdapter(10, runner, model="gemini-explicito").invoke(
        "revise", tmp_path, {}
    )

    assert runner.arguments[runner.arguments.index("--model") + 1] == "gemini-explicito"


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


@pytest.mark.parametrize(
    ("branches", "document_name", "document", "expected"),
    [
        (("main",), None, "", "main"),
        (
            ("develop", "main"),
            "AGENTS.md",
            "O fluxo de branches usa develop.",
            "develop",
        ),
        (
            ("develop", "main"),
            "CONTRIBUTING.md",
            "Pull Request tem base develop.",
            "develop",
        ),
        (("develop", "main"), None, "", "main"),
    ],
)
def test_descoberta_de_branches_e_documentacao_sem_executar_texto(
    tmp_path: Path,
    branches: tuple[str, ...],
    document_name: str | None,
    document: str,
    expected: str,
) -> None:
    if document_name:
        (tmp_path / document_name).write_text(document, encoding="utf-8")
    refs = "\n".join((*branches, *(f"origin/{branch}" for branch in branches)))
    results = {
        ("git", "rev-parse", "--show-toplevel"): CommandResult(0, str(tmp_path)),
        ("git", "remote"): CommandResult(0, "origin\n"),
        ("git", "remote", "get-url", "origin"): CommandResult(
            0, "https://github.com/acme/repo.git\n"
        ),
        (
            "git",
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes",
        ): CommandResult(0, refs),
        (
            "gh",
            "repo",
            "view",
            "acme/repo",
            "--json",
            "defaultBranchRef",
        ): CommandResult(0, '{"defaultBranchRef":{"name":"main"}}'),
        ("gh", "project", "list", "--owner", "acme", "--format", "json"): CommandResult(
            0, '{"projects":[{"number":7}]}'
        ),
        ("agy", "models"): CommandResult(0, "gemini-model-a\n"),
    }
    runner = CwdRunner(results)

    discovery = ProjectInitService(runner).discover(tmp_path)

    assert discovery.branches == branches
    assert discovery.suggested_base_branch == expected
    assert discovery.github_projects == (7,)
    assert discovery.gemini_models == ("gemini-model-a",)
    assert all(call[0] in {"git", "gh", "agy"} for call in runner.calls)


def test_base_target_independentes_e_branch_protegida_falha_antes_de_io(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.workspace.base_branch == "develop"
    assert config.github.pull_request_target == "develop"
    changed = config.model_copy(
        update={
            "github": config.github.model_copy(
                update={"pull_request_target": "release"}
            )
        }
    )
    assert changed.workspace.base_branch == "develop"
    assert changed.github.pull_request_target == "release"

    pipeline = RunPipeline(config, object(), object(), object(), object(), object())
    with pytest.raises(RunPipelineError, match="protegida"):
        pipeline.run(44, "main")


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


def test_supervisor_converte_quota_de_nova_execucao_em_espera(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)

    class Work:
        calls = 0

        def work(self):
            self.calls += 1
            run = store.create(
                44,
                branch="work/nova",
                worktree_path=str(tmp_path / "wt"),
                base_ref="develop",
            )
            run = store.transition(
                run.id, ExecutionPhase.CODEX_RUNNING, summary="Codex iniciado"
            )
            store.transition(
                run.id,
                ExecutionPhase.WAITING_CODEX_QUOTA,
                summary="quota",
                quota_provider="codex",
                quota_classification="TRANSIENT_RATE_LIMIT",
                quota_observed_at=datetime.now(timezone.utc).isoformat(),
                quota_retry_at=retry_at.isoformat(),
            )
            raise RunPipelineError("quota checkpointada")

    work = Work()
    sleeps: list[float] = []

    def interrupt(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SupervisorService(config, work, store, interrupt).watch()

    active = store.list_active()
    assert work.calls == 1
    assert len(active) == 1
    assert active[0].phase == ExecutionPhase.WAITING_CODEX_QUOTA
    assert sleeps == [config.supervisor.max_sleep_seconds]


def test_supervisor_nao_converte_erro_sem_checkpoint_de_quota(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)

    class Work:
        def work(self):
            raise RunPipelineError("falha terminal")

    with pytest.raises(RunPipelineError, match="terminal"):
        SupervisorService(config, Work(), store, lambda _: None).watch()


@pytest.mark.parametrize(
    "kind", [ProviderFailureKind.AUTH_ERROR, ProviderFailureKind.MODEL_UNAVAILABLE]
)
def test_auth_e_modelo_indisponivel_falham_terminalmente(
    tmp_path: Path, kind: ProviderFailureKind
) -> None:
    config = _config(tmp_path)
    store = SqliteExecutionStore(config.state.database_path)
    run = store.create(
        44,
        branch="work/provider",
        worktree_path=str(tmp_path / "wt"),
        base_ref="develop",
    )
    store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="provider")
    pipeline = RunPipeline(
        config, object(), object(), object(), object(), object(), execution_store=store
    )
    pipeline._execution_id = run.id
    failure = ProviderFailure(
        "codex", kind, "falha controlada", datetime.now(timezone.utc)
    )

    with pytest.raises(RunPipelineError):
        pipeline._record_provider_wait(failure, ExecutionPhase.WAITING_CODEX_QUOTA)

    latest = store.get(run.id)
    assert latest.phase == ExecutionPhase.FAILED
    assert latest.quota_classification == kind.value


def test_quota_gemini_preserva_head_pr_sessao_e_nao_chama_codex(
    tmp_path: Path,
) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    head = "a" * 40
    run = store.create(
        44,
        branch="work/review",
        worktree_path=str(tmp_path / "wt"),
        base_ref="develop",
    )
    run = store.transition(
        run.id,
        ExecutionPhase.CODEX_RUNNING,
        summary="codex",
        codex_session_id="session-44",
        current_head_sha=head,
    )
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="gates")
    run = store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary="commit")
    run = store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary="push")
    run = store.transition(run.id, ExecutionPhase.PR_PENDING, summary="pr")
    run = store.transition(
        run.id,
        ExecutionPhase.WAITING_CI,
        summary="ci",
        pull_request_number=48,
        pull_request_url="https://github.com/acme/repo/pull/48",
    )
    run = store.transition(
        run.id,
        ExecutionPhase.GEMINI_REVIEWING,
        summary="review",
        ci_head_sha=head,
    )
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    store.transition(
        run.id,
        ExecutionPhase.WAITING_GEMINI_QUOTA,
        summary="quota",
        quota_provider="gemini",
        quota_classification="TRANSIENT_RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=retry_at.isoformat(),
    )

    class Never:
        def __getattr__(self, _name):
            raise AssertionError("nenhum efeito, inclusive Codex, deveria ser chamado")

    result = ResumeService(store, Never(), Never(), Never()).resume(44)
    preserved = store.get(run.id)

    assert result.phase == "WAITING_GEMINI_QUOTA"
    assert preserved.current_head_sha == head
    assert preserved.pull_request_number == 48
    assert preserved.codex_session_id == "session-44"


def test_retry_gemini_reabre_mesmo_run_no_head_exato_sem_mutacao(
    tmp_path: Path,
) -> None:
    store = SqliteExecutionStore(tmp_path / "state.db")
    head = "b" * 40
    run = store.create(
        44,
        branch="work/review",
        worktree_path=str(tmp_path / "wt"),
        base_ref="develop",
    )
    run = store.transition(
        run.id,
        ExecutionPhase.CODEX_RUNNING,
        summary="codex",
        codex_session_id="session-44",
        current_head_sha=head,
    )
    for phase in (
        ExecutionPhase.TESTING,
        ExecutionPhase.COMMIT_PENDING,
        ExecutionPhase.PUSH_PENDING,
        ExecutionPhase.PR_PENDING,
    ):
        run = store.transition(run.id, phase, summary="checkpoint")
    run = store.transition(
        run.id,
        ExecutionPhase.WAITING_CI,
        summary="ci",
        pull_request_number=48,
        pull_request_url="https://github.com/acme/repo/pull/48",
    )
    run = store.transition(
        run.id, ExecutionPhase.GEMINI_REVIEWING, summary="review", ci_head_sha=head
    )
    store.transition(
        run.id,
        ExecutionPhase.WAITING_GEMINI_QUOTA,
        summary="quota",
        quota_provider="gemini",
        quota_classification="TRANSIENT_RATE_LIMIT",
        quota_observed_at=datetime.now(timezone.utc).isoformat(),
        quota_retry_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    observation = RecoveryObservation(
        WorktreeState.CONVERGENT,
        local_head_sha=head,
        remote_head_sha=head,
        pull_requests=(
            PullRequestObservation(
                48,
                "https://github.com/acme/repo/pull/48",
                "acme/repo",
                "develop",
                "work/review",
                head,
                PullRequestState.OPEN,
            ),
        ),
        ci=CiObservation(CiState.SUCCESS, head),
    )

    class Observer:
        calls = 0

        def observe(self, observed_run):
            self.calls += 1
            assert observed_run.id == run.id
            assert observed_run.phase == ExecutionPhase.GEMINI_REVIEWING
            assert observed_run.current_head_sha == head
            return observation

    class ReviewOnlyEffects:
        calls: list[str] = []

        def review_head(self, reviewed_run, prior_findings):
            self.calls.append("review")
            assert reviewed_run.id == run.id
            assert reviewed_run.current_head_sha == head
            assert prior_findings == ()
            return StructuredReview(ReviewVerdict.APPROVED, (), head, "aprovado")

    observer = Observer()
    effects = ReviewOnlyEffects()
    policy = RecoveryPolicy("acme/repo", "develop", False, 3)
    result = ResumeService(
        store,
        observer,
        RecoveryPlanner(policy),
        RecoveryExecutor(policy, store, effects),
    ).resume(44)

    resumed = store.get(run.id)
    assert observer.calls == 2
    assert effects.calls == ["review"]
    assert result.execution_id == run.id
    assert result.phase == "APPROVED_AWAITING_ACTION"
    assert resumed.id == run.id
    assert resumed.phase == ExecutionPhase.APPROVED_AWAITING_ACTION
    assert resumed.current_head_sha == head
    assert resumed.pull_request_number == 48


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


def test_init_ambiguo_exige_confirmacao_de_base_e_target(
    tmp_path: Path, monkeypatch
) -> None:
    discovery = ProjectDiscovery(
        tmp_path,
        "origin",
        "https://github.com/acme/repo.git",
        "acme",
        "repo",
        "main",
        ("develop", "main"),
        "main",
        (),
        (1,),
        ("origin",),
    )
    monkeypatch.setattr(ProjectInitService, "discover", lambda self, cwd: discovery)

    result = CliRunner().invoke(app, ["init"], input="develop\nmain\n\n\n\nN\n")

    assert "Base das novas branches" in result.output
    assert "Destino dos Pull Requests" in result.output
    assert result.exit_code != 0
    assert not (tmp_path / "orchestrator.toml").exists()


def test_reconfiguracao_preserva_opcoes_nao_alteradas(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    existing = _config(tmp_path, codex_model="codex-x", gemini_model="gemini-x")
    existing = existing.model_copy(
        update={
            "github": existing.github.model_copy(
                update={"ready_status": "Pronto", "done_status": "Concluído"}
            ),
            "execution": existing.execution.model_copy(update={"auto_merge": True}),
            "ci": existing.ci.model_copy(update={"required_checks": ("lint", "test")}),
        }
    )
    ProjectInitService().write(repository / "orchestrator.toml", existing)
    discovery = ProjectDiscovery(
        repository,
        "origin",
        "https://github.com/acme/repo.git",
        "acme",
        "repo",
        "main",
        ("develop", "main"),
        "develop",
        ("AGENTS.md indica fluxo baseado em develop",),
        (1,),
        ("origin",),
    )
    monkeypatch.setattr(ProjectInitService, "discover", lambda self, cwd: discovery)

    result = CliRunner().invoke(app, ["init"], input="\n\n\n\n\n\n")

    assert result.exit_code == 0, result.output
    loaded = load_config(repository / "orchestrator.toml")
    assert loaded.github.ready_status == "Pronto"
    assert loaded.github.done_status == "Concluído"
    assert loaded.execution.auto_merge is True
    assert loaded.ci.required_checks == ("lint", "test")
    assert loaded.providers.codex_model == "codex-x"
