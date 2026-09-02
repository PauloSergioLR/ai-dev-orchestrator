"""Carregamento e validação da configuração local do orquestrador."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


DEFAULT_CONFIG_PATH = Path("orchestrator.toml")


class ConfigurationError(Exception):
    """Indica que a configuração local não pôde ser carregada."""


class GitHubConfig(BaseModel):
    """Configuração de identificação do repositório no GitHub."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    project_number: int = Field(gt=0)
    ready_status: str = Field(min_length=1)
    in_progress_status: str = Field(default="In Progress", min_length=1)
    ai_review_status: str = Field(default="AI Review", min_length=1)
    pull_request_base: str = Field(default="main", min_length=1)
    status_field_name: str = Field(default="Status", min_length=1)

    @property
    def repository_full_name(self) -> str:
        """Retorna o identificador completo do repositório no GitHub."""
        return f"{self.owner}/{self.repository}"


class ExecutionConfig(BaseModel):
    """Limites locais para as futuras execuções do orquestrador."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(gt=0)
    max_parallel_runs: int = Field(gt=0)
    auto_merge: StrictBool
    merge_timeout_seconds: float = Field(default=30, gt=0)


class CiConfig(BaseModel):
    """Política local para aguardar os checks obrigatórios do Pull Request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    required_checks: tuple[str, ...] = ("test",)
    poll_interval_seconds: float = Field(default=5, gt=0)
    timeout_seconds: float = Field(default=900, gt=0)

    @field_validator("required_checks")
    @classmethod
    def required_checks_must_be_unambiguous(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("deve conter ao menos um check obrigatório")
        if any(not name for name in value):
            raise ValueError("não pode conter nomes vazios")
        if len(set(value)) != len(value):
            raise ValueError("não pode conter checks duplicados")
        return value


class ReviewConfig(BaseModel):
    """Política local do reviewer independente."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(default="antigravity", min_length=1)
    timeout_seconds: float = Field(default=900, gt=0)
    max_correction_attempts: int = Field(default=3, gt=0)
    blocking_severities: tuple[str, ...] = ("CRITICAL", "HIGH", "MEDIUM")

    @field_validator("provider")
    @classmethod
    def provider_must_be_supported(cls, value: str) -> str:
        if value != "antigravity":
            raise ValueError("provider deve ser 'antigravity'")
        return value

    @field_validator("blocking_severities")
    @classmethod
    def severities_must_be_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        if not value or any(severity not in allowed for severity in value):
            raise ValueError("deve conter severidades válidas e não pode ser vazio")
        if len(set(value)) != len(value):
            raise ValueError("não pode conter severidades duplicadas")
        return value


class WorkspaceConfig(BaseModel):
    """Locais e referência usados para preparar um worktree."""

    model_config = ConfigDict(extra="forbid")

    repository_path: Path
    worktrees_dir: Path
    base_ref: str = Field(min_length=1)
    remote_name: str = Field(default="origin", min_length=1)

    @field_validator("repository_path", "worktrees_dir")
    @classmethod
    def paths_must_be_absolute(cls, value: Path) -> Path:
        """Recusa paths relativos para não depender do cwd do processo."""
        if not value.is_absolute():
            raise ValueError("deve ser um caminho absoluto")
        return value


class _EnvironmentSettingsSource(PydanticBaseSettingsSource):
    """Converte o booleano textual de ambiente sem relaxar o TOML."""

    def __init__(
        self, settings_cls: type[BaseSettings], source: PydanticBaseSettingsSource
    ) -> None:
        super().__init__(settings_cls)
        self.source = source

    def get_field_value(
        self, field: Any, field_name: str
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data = self.source()
        execution = data.get("execution")
        if isinstance(execution, dict):
            auto_merge = execution.get("auto_merge")
            if isinstance(auto_merge, str) and auto_merge.lower() in {"true", "false"}:
                execution["auto_merge"] = auto_merge.lower() == "true"
        return data


class OrchestratorConfig(BaseSettings):
    """Configuração validada, composta por TOML e variáveis de ambiente."""

    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    github: GitHubConfig
    execution: ExecutionConfig
    workspace: WorkspaceConfig
    ci: CiConfig = Field(default_factory=CiConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Define ambiente como fonte prioritária e exclui suporte a .env."""
        return (
            _EnvironmentSettingsSource(settings_cls, env_settings),
            init_settings,
        )


def load_config(path: Path | str | None = None) -> OrchestratorConfig:
    """Carrega a configuração do caminho informado ou de ``orchestrator.toml``.

    Variáveis com o prefixo ``ORCH_`` sobrescrevem os respectivos valores do TOML.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    try:
        with config_path.open("rb") as config_file:
            toml_data = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Arquivo de configuração não encontrado: {config_path}"
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"Arquivo TOML inválido: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Não foi possível ler o arquivo de configuração: {config_path}"
        ) from error

    try:
        return OrchestratorConfig(**toml_data)
    except ValidationError as error:
        first_error = error.errors()[0]
        field = ".".join(str(part) for part in first_error["loc"])
        detail = first_error["msg"]
        raise ConfigurationError(
            f"Configuração inválida no campo '{field}': {detail}"
        ) from error
