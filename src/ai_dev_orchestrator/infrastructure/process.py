"""Execução segura e reutilizável de processos locais."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
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

    def run(
        self,
        arguments: Sequence[str],
        cwd: str | Path | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        """Executa argumentos de processo e normaliza falhas esperadas."""
        command = list(arguments)
        command_name = command[0]
        executable = shutil.which(command_name)
        if executable is None:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {command_name}",
            )
        command[0] = executable
        try:
            input_bytes = (
                input_text.encode("utf-8", errors="strict")
                if input_text is not None
                else None
            )
        except UnicodeEncodeError as error:
            return CommandResult(
                returncode=None,
                error=f"Falha ao codificar entrada textual como UTF-8: {error}",
            )
        try:
            options = {
                "capture_output": True,
                "timeout": self.timeout,
                "shell": False,
                "check": False,
            }
            if cwd is not None:
                options["cwd"] = cwd
            if input_bytes is not None:
                options["input"] = input_bytes
            completed = subprocess.run(command, **options)
        except FileNotFoundError:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {command_name}",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                returncode=None,
                error=f"Comando excedeu o timeout de {self.timeout:g}s",
            )
        except OSError as error:
            return CommandResult(returncode=None, error=str(error))

        try:
            stdout = completed.stdout.decode("utf-8", errors="strict")
            stderr = completed.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            return CommandResult(
                returncode=None,
                error=f"Falha ao decodificar saída do comando como UTF-8: {error}",
            )

        return CommandResult(
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
