"""Execução segura e reutilizável de processos locais."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Sequence


COMMAND_TIMEOUT_SECONDS = 5


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
