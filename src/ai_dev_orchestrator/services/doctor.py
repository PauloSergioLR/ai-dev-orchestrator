"""Diagnóstico local dos pré-requisitos do orquestrador."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from ai_dev_orchestrator.config import ConfigurationError, load_config


COMMAND_TIMEOUT_SECONDS = 5


class CheckStatus(StrEnum):
    """Estado de uma verificação do diagnóstico."""

    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class DoctorCheck:
    """Resultado estruturado de uma verificação."""

    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class CommandResult:
    """Resultado de um comando externo executado localmente."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Indica se o processo terminou com sucesso."""
        return self.returncode == 0


class CommandRunner:
    """Executa comandos locais com timeout e sem shell."""

    def __init__(self, timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def run(self, arguments: Sequence[str]) -> CommandResult:
        """Executa argumentos de processo e normaliza falhas esperadas."""
        try:
            completed = subprocess.run(
                list(arguments),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {arguments[0]}",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                returncode=None,
                error=f"Comando excedeu o timeout de {self.timeout:g}s",
            )
        except OSError as error:
            return CommandResult(returncode=None, error=str(error))

        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class DoctorService:
    """Agrupa verificações locais e sem efeitos colaterais."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.config_path = config_path

    def diagnose(self) -> list[DoctorCheck]:
        """Executa todas as verificações obrigatórias do comando doctor."""
        return [
            self._check_python(),
            self._check_command("Git", ["git", "--version"]),
            self._check_github_cli(),
            self._check_command("Codex CLI", ["codex", "--version"]),
            self._check_command("Gemini CLI", ["gemini", "--version"]),
            self._check_repository(),
            self._check_configuration(),
        ]

    def _check_python(self) -> DoctorCheck:
        version = sys.version_info
        current = f"{version.major}.{version.minor}.{version.micro}"
        if version.major == 3 and version.minor == 13:
            return DoctorCheck("Python", CheckStatus.OK, current)
        return DoctorCheck(
            "Python", CheckStatus.ERROR, f"{current}; é necessário Python 3.13.x"
        )

    def _check_command(self, name: str, arguments: Sequence[str]) -> DoctorCheck:
        result = self.runner.run(arguments)
        if result.error:
            return DoctorCheck(name, CheckStatus.ERROR, result.error)
        if not result.succeeded:
            return DoctorCheck(name, CheckStatus.ERROR, self._command_failure(result))
        version = result.stdout.strip() or "disponível"
        return DoctorCheck(name, CheckStatus.OK, version)

    def _check_github_cli(self) -> DoctorCheck:
        result = self.runner.run(["gh", "auth", "status"])
        if result.error:
            return DoctorCheck("GitHub CLI", CheckStatus.ERROR, result.error)
        if not result.succeeded:
            return DoctorCheck("GitHub CLI", CheckStatus.ERROR, "gh não está autenticado")
        return DoctorCheck("GitHub CLI", CheckStatus.OK, "autenticado")

    def _check_repository(self) -> DoctorCheck:
        repository = self.runner.run(["git", "rev-parse", "--is-inside-work-tree"])
        if repository.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, repository.error)
        if not repository.succeeded or repository.stdout.strip() != "true":
            return DoctorCheck("Repository", CheckStatus.ERROR, "diretório não é um repositório Git")

        remote = self.runner.run(["git", "remote"])
        if remote.error:
            return DoctorCheck("Repository", CheckStatus.ERROR, remote.error)
        if not remote.succeeded:
            return DoctorCheck("Repository", CheckStatus.ERROR, self._command_failure(remote))
        if not remote.stdout.strip():
            return DoctorCheck("Repository", CheckStatus.ERROR, "nenhum remote configurado")
        return DoctorCheck("Repository", CheckStatus.OK, "remote configurado")

    def _check_configuration(self) -> DoctorCheck:
        try:
            load_config(self.config_path)
        except ConfigurationError as error:
            return DoctorCheck("Configuration", CheckStatus.ERROR, str(error))
        return DoctorCheck("Configuration", CheckStatus.OK, "orchestrator.toml válido")

    @staticmethod
    def _command_failure(result: CommandResult) -> str:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            return f"comando retornou código {result.returncode}: {detail}"
        return f"comando retornou código {result.returncode}"


def has_errors(checks: Sequence[DoctorCheck]) -> bool:
    """Indica se algum resultado impede a execução do ambiente."""
    return any(check.status is CheckStatus.ERROR for check in checks)
