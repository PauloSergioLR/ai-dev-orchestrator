"""Provas locais das fronteiras Git, cleanup, configuração e diagnósticos."""

import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter, GitWorktreeError
from ai_dev_orchestrator.adapters.github import GitHubPullRequestAdapter, GitHubPullRequestError
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.infrastructure.redaction import sanitize_diagnostic
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.services.cleanup import CleanupService
from ai_dev_orchestrator.services.init_project import _parse_github_remote, render_toml
from test_cleanup_history import FakeGit, completed, config


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *arguments],
                            capture_output=True, check=True, timeout=20)
    return result.stdout.decode("utf-8").strip()


def test_exclusao_git_condicionada_preserva_branch_avancada(short_git_tmp_path):
    tmp_path = short_git_tmp_path
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    remote.mkdir()
    git(repo, "init", "-b", "main")
    git(remote, "init", "--bare")
    git(repo, "config", "user.name", "Teste local")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-b", "work/topic")
    git(repo, "push", "origin", "work/topic")
    (repo / "file.txt").write_text("avanço posterior à observação", encoding="utf-8")
    git(repo, "commit", "-am", "avanco")
    advanced = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "work/topic")
    git(repo, "checkout", "main")
    adapter = GitWorktreeAdapter()
    with pytest.raises(GitWorktreeError):
        adapter.delete_remote_branch(repo, "origin", "work/topic", base)
    assert git(remote, "rev-parse", "refs/heads/work/topic") == advanced
    with pytest.raises(GitWorktreeError):
        adapter.delete_local_branch(repo, "work/topic", base)
    assert git(repo, "rev-parse", "refs/heads/work/topic") == advanced
    adapter.delete_remote_branch(repo, "origin", "work/topic", advanced)
    assert not adapter.remote_branch_exists(repo, "origin", "work/topic")


@pytest.mark.parametrize("outside", [True, False])
def test_cleanup_preserva_caminho_externo_e_branch_reutilizada(tmp_path, outside):
    store = SqliteExecutionStore(tmp_path / "state.db")
    path = tmp_path / ("external" if outside else "worktrees/topic")
    path.mkdir(parents=True)
    sentinel = path / "preservar.txt"
    sentinel.write_text("conteúdo do usuário", encoding="utf-8")
    run = completed(store, path=str(path))

    class ChangedGit(FakeGit):
        def local_branch_head(self, *_):
            return "b" * 40

    adapter = ChangedGit()
    result = CleanupService(config(tmp_path), store, adapter).cleanup(run.id)
    assert result.status in {"PENDING", "PRESERVED"}
    assert sentinel.read_text(encoding="utf-8") == "conteúdo do usuário"
    assert adapter.calls == []


@pytest.mark.parametrize("url", ["https://evilgithub.com/a/b", "https://github.com.evil/a/b", "https://github.com/a/b?token=x", "git@evilgithub.com:a/b"])
def test_owner_do_remote_nao_e_inferido_por_substring(url):
    assert _parse_github_remote(url) == (None, None)


def test_toml_roundtrip_preserva_configuracao_completa(tmp_path):
    original = config(tmp_path, remove_local_branch=True)
    original.github.project_timeout_seconds = 123
    original.review.executable = "C:/custom tools/agy.exe"
    original.providers.codex_timeout_seconds = 5400
    original.execution.max_no_changes_attempts = 2
    original.code_review_graph.enabled = True  # Somente serialização; nenhum processo.
    rendered = render_toml(original)
    recovered = OrchestratorConfig.model_validate(tomllib.loads(rendered))
    assert original.model_dump() == recovered.model_dump()
    assert "required_checks" not in recovered.ci.model_fields_set


@pytest.mark.parametrize("payload", [[], {"number": True}, {"number": 99}])
def test_snapshot_pr_malformado_falha_controladamente(tmp_path, payload):
    class Runner:
        def run(self, *_args, **_kwargs):
            return CommandResult(0, json.dumps(payload))

    with pytest.raises(GitHubPullRequestError):
        GitHubPullRequestAdapter(config(tmp_path), Runner()).get_merge_snapshot(42)


def test_review_recusa_diff_coletado_entre_heads(tmp_path):
    metadata = {"number": 42, "url": "https://github.com/o/r/pull/42", "state": "OPEN",
                "baseRefName": "main", "baseRefOid": "c" * 40, "headRefName": "work/topic",
                "headRefOid": "a" * 40, "changedFiles": 1, "files": [{"path": "file.txt"}]}
    results = [CommandResult(0, json.dumps(metadata)),
               CommandResult(0, json.dumps([[{"sha": "a" * 40}]])),
               CommandResult(0, "diff --git a/file.txt b/file.txt\n+novo"),
               CommandResult(0, json.dumps({**metadata, "headRefOid": "b" * 40}))]

    class Runner:
        def run(self, *_args, **_kwargs):
            return results.pop(0)

    with pytest.raises(GitHubPullRequestError, match="mudou durante"):
        GitHubPullRequestAdapter(config(tmp_path), Runner()).get_review_data(42)


@pytest.mark.parametrize("destination", ["https://github.com/wrong/repo.git", "https://evilgithub.com/o/r.git", "https://github.com/o/r.git\nhttps://github.com/wrong/repo.git"])
def test_remote_fetch_e_push_exigem_identidade_exata(destination, tmp_path):
    class Runner:
        def run(self, arguments, **kwargs):
            return CommandResult(0, destination if "--push" in arguments else "git@github.com:o/r.git")

    with pytest.raises(GitWorktreeError):
        GitWorktreeAdapter(Runner()).verify_remote_identity(tmp_path, "origin", "o/r")


def test_diagnosticos_redigem_chaves_e_url_com_credenciais(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "synthetic-private-value")
    output = sanitize_diagnostic("synthetic-private-value api_key=another-value https://user:password@example.invalid/path")
    assert "synthetic-private-value" not in output
    assert "another-value" not in output
    assert "user:password" not in output
    assert "[redigido]" in output


def test_cleanup_git_preserva_arquivos_ignorados(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text("private.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "-c", "user.name=Teste", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    sentinel = repo / "private.txt"
    sentinel.write_text("conteúdo ignorado a preservar", encoding="utf-8")
    assert not git(repo, "status", "--porcelain")
    assert not GitWorktreeAdapter().worktree_is_clean(repo, repo)
    assert sentinel.exists()


@pytest.mark.parametrize("header", ['Authorization: Bearer private-value', '{"Authorization": "Bearer private-value"}', "authorization='Basic private-value'"])
def test_authorization_entre_aspas_nao_expoe_valor(header):
    assert "private-value" not in sanitize_diagnostic(header)
