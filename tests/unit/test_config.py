"""Testes do carregamento de configuração."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_orchestrator.config import ConfigurationError, OrchestratorConfig, load_config


def valid_toml(tmp_path: Path) -> str:
    """Produz TOML com paths absolutos nativos da plataforma em execução."""
    return f'''
[github]
owner = "acme"
repository = "orchestrator"
project_number = 42
ready_status = "Ready"

[workspace]
repository_path = "{(tmp_path / "repository").as_posix()}"
worktrees_dir = "{(tmp_path / "worktrees").as_posix()}"
base_ref = "main"

[execution]
max_attempts = 2
max_parallel_runs = 1
auto_merge = false
'''


def write_config(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_a_valid_toml(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))

    assert config.github.owner == "acme"
    assert config.github.project_number == 42
    assert config.execution.auto_merge is False
    assert (config.workspace.remote_name, config.github.pull_request_base, config.github.ai_review_status) == ("origin", "main", "AI Review")


def test_accepts_absolute_workspace_paths(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))

    assert config.workspace.repository_path.is_absolute()
    assert config.workspace.worktrees_dir.is_absolute()


def test_allows_valid_direct_instantiation(tmp_path: Path) -> None:
    config = OrchestratorConfig(
        github={"owner": "acme", "repository": "orchestrator", "project_number": 42,
                "ready_status": "Ready"},
        execution={"max_attempts": 2, "max_parallel_runs": 1, "auto_merge": False},
        workspace={"repository_path": tmp_path / "repository",
                   "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
    )

    assert config.github.owner == "acme"


def test_rejects_extra_argument_in_direct_instantiation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OrchestratorConfig(
            github={"owner": "acme", "repository": "orchestrator", "project_number": 42,
                    "ready_status": "Ready"},
            execution={"max_attempts": 2, "max_parallel_runs": 1, "auto_merge": False},
            workspace={"repository_path": tmp_path / "repository",
                       "worktrees_dir": tmp_path / "worktrees", "base_ref": "main"},
            unexpected=True,
        )


def test_uses_orchestrator_toml_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path / "orchestrator.toml", valid_toml(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert load_config().github.repository == "orchestrator"


def test_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="não encontrado") as error:
        load_config(tmp_path / "missing.toml")

    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_reports_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path / "invalid.toml", "[github\nowner = 'acme'")

    with pytest.raises(ConfigurationError, match="TOML inválido") as error:
        load_config(path)

    assert error.value.__cause__ is not None


@pytest.mark.parametrize("change", ["missing", "project_number", "auto_merge"])
def test_rejects_missing_or_invalid_values(tmp_path: Path, change: str) -> None:
    content = valid_toml(tmp_path)
    if change == "missing":
        content = "[github]\nowner = 'acme'\n"
    elif change == "project_number":
        content = content.replace("project_number = 42", "project_number = 0")
    else:
        content = content.replace("auto_merge = false", 'auto_merge = "false"')

    with pytest.raises(ConfigurationError, match="Configuração inválida") as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert error.value.__cause__ is not None


def test_validation_error_identifies_invalid_field(tmp_path: Path) -> None:
    content = valid_toml(tmp_path).replace("project_number = 42", "project_number = 0")

    with pytest.raises(ConfigurationError, match="github.project_number") as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert isinstance(error.value.__cause__, ValidationError)


@pytest.mark.parametrize("field", ["repository_path", "worktrees_dir"])
def test_rejects_relative_workspace_path_identifying_the_field(
    tmp_path: Path, field: str
) -> None:
    content = valid_toml(tmp_path)
    valid_path = (tmp_path / ("repository" if field == "repository_path" else "worktrees")).as_posix()
    content = content.replace(f'{field} = "{valid_path}"', f'{field} = "relative/{field}"')

    with pytest.raises(ConfigurationError, match=f"workspace.{field}") as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert "caminho absoluto" in str(error.value)


def test_rejects_unknown_fields(tmp_path: Path) -> None:
    content = valid_toml(tmp_path).replace('owner = "acme"', 'owner = "acme"\nunexpected = true')

    with pytest.raises(ConfigurationError, match="Configuração inválida"):
        load_config(write_config(tmp_path / "config.toml", content))


def test_environment_variables_override_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_GITHUB__OWNER", "environment-owner")
    monkeypatch.setenv("ORCH_EXECUTION__MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ORCH_EXECUTION__AUTO_MERGE", "true")

    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))

    assert config.github.owner == "environment-owner"
    assert config.execution.max_attempts == 3
    assert config.execution.auto_merge is True


def test_environment_overrides_publication_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_WORKSPACE__REMOTE_NAME", "upstream")
    monkeypatch.setenv("ORCH_GITHUB__PULL_REQUEST_BASE", "release")
    monkeypatch.setenv("ORCH_GITHUB__AI_REVIEW_STATUS", "Revisão IA")
    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))
    assert (config.workspace.remote_name, config.github.pull_request_base, config.github.ai_review_status) == ("upstream", "release", "Revisão IA")


def test_ci_defaults_and_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_CI__REQUIRED_CHECKS", '["unit", "integration"]')
    monkeypatch.setenv("ORCH_CI__POLL_INTERVAL_SECONDS", "2")
    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))
    assert config.ci.required_checks == ("unit", "integration")
    assert config.ci.poll_interval_seconds == 2
    assert config.ci.timeout_seconds == 900


def test_convergence_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_CONVERGENCE__POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("ORCH_CONVERGENCE__TIMEOUT_SECONDS", "12")
    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))
    assert config.convergence.poll_interval_seconds == 0.5
    assert config.convergence.timeout_seconds == 12


def test_review_correction_attempts_default_and_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_config(write_config(tmp_path / "defaults.toml", valid_toml(tmp_path))).convergence.timeout_seconds == 30
    assert load_config(write_config(tmp_path / "default.toml", valid_toml(tmp_path))).review.max_correction_attempts == 3
    monkeypatch.setenv("ORCH_REVIEW__MAX_CORRECTION_ATTEMPTS", "4")
    assert load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path))).review.max_correction_attempts == 4


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_rejects_invalid_review_correction_attempts(tmp_path: Path, value: str) -> None:
    content = valid_toml(tmp_path) + f"\n[review]\nmax_correction_attempts = {value if value != 'not-a-number' else repr(value)}\n"
    with pytest.raises(ConfigurationError, match="review.max_correction_attempts"):
        load_config(write_config(tmp_path / "config.toml", content))


@pytest.mark.parametrize("ci", ["required_checks = []", "poll_interval_seconds = 0", "timeout_seconds = -1"])
def test_rejects_invalid_ci_configuration(tmp_path: Path, ci: str) -> None:
    content = valid_toml(tmp_path) + f"\n[ci]\n{ci}\n"
    with pytest.raises(ConfigurationError, match="ci"):
        load_config(write_config(tmp_path / "config.toml", content))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_rejects_invalid_convergence_configuration(
    tmp_path: Path, value: str
) -> None:
    content = valid_toml(tmp_path) + f"\n[convergence]\ntimeout_seconds = {value}\n"
    with pytest.raises(ConfigurationError, match="convergence.timeout_seconds"):
        load_config(write_config(tmp_path / "config.toml", content))


def test_environment_variables_have_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_GITHUB__REPOSITORY", "environment-repository")

    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))

    assert config.github.repository == "environment-repository"


def test_environment_variables_override_absolute_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository-from-environment"
    worktrees = tmp_path / "worktrees-from-environment"
    monkeypatch.setenv("ORCH_WORKSPACE__REPOSITORY_PATH", str(repository))
    monkeypatch.setenv("ORCH_WORKSPACE__WORKTREES_DIR", str(worktrees))

    config = load_config(write_config(tmp_path / "config.toml", valid_toml(tmp_path)))

    assert config.workspace.repository_path == repository
    assert config.workspace.worktrees_dir == worktrees
