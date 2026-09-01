"""Testes do carregamento de configuração."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_orchestrator.config import (
    ConfigurationError,
    OrchestratorConfig,
    load_config,
)


VALID_TOML = """
[github]
owner = "acme"
repository = "orchestrator"
project_number = 42
ready_status = "Ready"

[workspace]
repository_path = "C:/repos/orchestrator"
worktrees_dir = "C:/repos/worktrees"
base_ref = "main"

[execution]
max_attempts = 2
max_parallel_runs = 1
auto_merge = false
"""


def write_config(path: Path, content: str = VALID_TOML) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_a_valid_toml(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "config.toml"))

    assert config.github.owner == "acme"
    assert config.github.project_number == 42
    assert config.execution.auto_merge is False


def test_accepts_absolute_workspace_paths(tmp_path: Path) -> None:
    content = VALID_TOML.replace("C:/repos/orchestrator", (tmp_path / "repository").as_posix())
    content = content.replace("C:/repos/worktrees", (tmp_path / "worktrees").as_posix())

    config = load_config(write_config(tmp_path / "config.toml", content))

    assert config.workspace.repository_path.is_absolute()
    assert config.workspace.worktrees_dir.is_absolute()


def test_allows_valid_direct_instantiation() -> None:
    config = OrchestratorConfig(
        github={
            "owner": "acme",
            "repository": "orchestrator",
            "project_number": 42,
            "ready_status": "Ready",
        },
        execution={
            "max_attempts": 2,
            "max_parallel_runs": 1,
            "auto_merge": False,
        },
        workspace={
            "repository_path": "C:/repos/orchestrator",
            "worktrees_dir": "C:/repos/worktrees",
            "base_ref": "main",
        },
    )

    assert config.github.owner == "acme"


def test_rejects_extra_argument_in_direct_instantiation() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OrchestratorConfig(
            github={
                "owner": "acme",
                "repository": "orchestrator",
                "project_number": 42,
                "ready_status": "Ready",
            },
            execution={
                "max_attempts": 2,
                "max_parallel_runs": 1,
                "auto_merge": False,
            },
            workspace={
                "repository_path": "C:/repos/orchestrator",
                "worktrees_dir": "C:/repos/worktrees",
                "base_ref": "main",
            },
            unexpected=True,
        )


def test_uses_orchestrator_toml_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path / "orchestrator.toml")
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


@pytest.mark.parametrize(
    "content",
    [
        "[github]\nowner = 'acme'\n",
        VALID_TOML.replace("project_number = 42", "project_number = 0"),
        VALID_TOML.replace("auto_merge = false", 'auto_merge = "false"'),
    ],
)
def test_rejects_missing_or_invalid_values(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigurationError, match="Configuração inválida") as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert error.value.__cause__ is not None


def test_validation_error_identifies_invalid_field(tmp_path: Path) -> None:
    content = VALID_TOML.replace("project_number = 42", "project_number = 0")

    with pytest.raises(
        ConfigurationError, match="github.project_number"
    ) as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert isinstance(error.value.__cause__, ValidationError)


@pytest.mark.parametrize("field", ["repository_path", "worktrees_dir"])
def test_rejects_relative_workspace_path_identifying_the_field(
    tmp_path: Path, field: str
) -> None:
    content = VALID_TOML.replace(f'{field} = "C:/repos/{"orchestrator" if field == "repository_path" else "worktrees"}"', f'{field} = "relative/{field}"')

    with pytest.raises(ConfigurationError, match=f"workspace.{field}") as error:
        load_config(write_config(tmp_path / "config.toml", content))

    assert "caminho absoluto" in str(error.value)


def test_rejects_unknown_fields(tmp_path: Path) -> None:
    content = VALID_TOML.replace('owner = "acme"', 'owner = "acme"\nunexpected = true')

    with pytest.raises(ConfigurationError, match="Configuração inválida"):
        load_config(write_config(tmp_path / "config.toml", content))


def test_environment_variables_override_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_GITHUB__OWNER", "environment-owner")
    monkeypatch.setenv("ORCH_EXECUTION__MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ORCH_EXECUTION__AUTO_MERGE", "true")

    config = load_config(write_config(tmp_path / "config.toml"))

    assert config.github.owner == "environment-owner"
    assert config.execution.max_attempts == 3
    assert config.execution.auto_merge is True


def test_environment_variables_have_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_GITHUB__REPOSITORY", "environment-repository")

    config = load_config(write_config(tmp_path / "config.toml"))

    assert config.github.repository == "environment-repository"


def test_environment_variables_override_absolute_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository-from-environment"
    worktrees = tmp_path / "worktrees-from-environment"
    monkeypatch.setenv("ORCH_WORKSPACE__REPOSITORY_PATH", str(repository))
    monkeypatch.setenv("ORCH_WORKSPACE__WORKTREES_DIR", str(worktrees))

    config = load_config(write_config(tmp_path / "config.toml"))

    assert config.workspace.repository_path == repository
    assert config.workspace.worktrees_dir == worktrees
