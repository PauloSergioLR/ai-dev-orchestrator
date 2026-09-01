"""Carregamento e validação da configuração local do orquestrador."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
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


class ExecutionConfig(BaseModel):
    """Limites locais para as futuras execuções do orquestrador."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(gt=0)
    max_parallel_runs: int = Field(gt=0)
    auto_merge: StrictBool


class _TomlSettingsSource(PydanticBaseSettingsSource):
    """Disponibiliza os dados de um TOML como fonte do Pydantic Settings."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self.data = data

    def get_field_value(
        self, field: Any, field_name: str
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self.data


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
        toml_data = init_settings.init_kwargs.get("_toml_data", {})
        return (
            _EnvironmentSettingsSource(settings_cls, env_settings),
            _TomlSettingsSource(settings_cls, toml_data),
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
        return OrchestratorConfig(_toml_data=toml_data)
    except ValidationError as error:
        raise ConfigurationError("Configuração inválida.") from error
