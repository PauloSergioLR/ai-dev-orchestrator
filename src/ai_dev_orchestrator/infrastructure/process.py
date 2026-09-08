"""Execução segura e reutilizável de processos locais."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import sys
import locale
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Sequence


COMMAND_TIMEOUT_SECONDS = 5


class OutputPolicy(StrEnum):
    SYSTEM_TEXT = "SYSTEM_TEXT"
    UTF8_STRICT = "UTF8_STRICT"
    BINARY = "BINARY"


class ProcessFailureKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
    OS_ERROR = "OS_ERROR"
    ENCODING_ERROR = "ENCODING_ERROR"
    PROCESS_CLEANUP_ERROR = "PROCESS_CLEANUP_ERROR"


class ProcessCleanupError(subprocess.TimeoutExpired):
    """Timeout cuja árvore de processos não pôde ser encerrada com segurança."""


def run_captured(command, *, capture_output, timeout, shell, check, cwd=None, input=None):
    """Captura bytes e encerra a árvore antes de permitir retry após timeout."""
    if shell or not capture_output or check:
        raise ValueError("Contrato de processo exige captura, shell=False e check=False")
    job = None
    launch_command = command
    options = {"start_new_session": True}
    if os.name == "nt":
        from ai_dev_orchestrator.infrastructure.windows_job import WindowsJob
        job = WindowsJob()
        launch_command = [sys.executable, str(Path(__file__).with_name("_process_child.py"))]
        header = json.dumps({"command": command, "has_input": input is not None}).encode("ascii")
        input = header + b"\n" + (input or b"")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        process = subprocess.Popen(launch_command, cwd=cwd, shell=False,
                                   stdin=subprocess.PIPE if input is not None else None,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        if job is not None:
            # O bootstrap só inicia o comando após receber o cabeçalho via stdin.
            try:
                job.assign(process)
            except OSError:
                process.kill()
                process.communicate(timeout=5)
                raise
    except BaseException:
        if job is not None:
            job.close()
        raise
    try:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            stopped = job.stop() if job is not None else _stop_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Um descendente escapou ou mantém os pipes abertos: não retentar.
                stopped = False
                stdout = getattr(error, "stdout", None) or b""
                stderr = getattr(error, "stderr", None) or b""
            if isinstance(error, KeyboardInterrupt) and stopped:
                raise
            failure = subprocess.TimeoutExpired if stopped else ProcessCleanupError
            raise failure(command, timeout, output=stdout, stderr=stderr) from error
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if job is not None:
            job.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _stop_process_tree(process: subprocess.Popen) -> bool:
    stopped = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        stopped = True
    except ProcessLookupError:
        stopped = True
    except (OSError, subprocess.TimeoutExpired):
        stopped = False
    # Mesmo quando a prova da árvore falha, tenta encerrar o processo direto.
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return stopped


def decode_output(data: bytes, policy: OutputPolicy, system_encoding: str) -> str:
    """Protocolos nunca usam fallback; texto humano preserva bytes não mapeáveis."""
    if policy == OutputPolicy.BINARY:
        return ""
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        if policy == OutputPolicy.UTF8_STRICT:
            raise
    return data.decode(system_encoding, errors="backslashreplace")


@dataclass(frozen=True)
class CommandResult:
    """Resultado de um comando externo executado localmente."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    failure_kind: ProcessFailureKind | None = None
    stdout_bytes: bytes = field(default=b"", repr=False)
    stderr_bytes: bytes = field(default=b"", repr=False)
    encoding_errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """Indica se o processo terminou com sucesso."""
        return self.returncode == 0 and self.error is None


class CommandRunner:
    """Executa comandos locais com timeout e sem shell."""

    def __init__(self, timeout: float = COMMAND_TIMEOUT_SECONDS, *,
                 system_encoding: str | None = None) -> None:
        self.timeout = timeout
        self.system_encoding = system_encoding or locale.getencoding()

    def run(
        self,
        arguments: Sequence[str],
        cwd: str | Path | None = None,
        input_text: str | None = None,
        *,
        stdout_policy: OutputPolicy = OutputPolicy.SYSTEM_TEXT,
        stderr_policy: OutputPolicy = OutputPolicy.SYSTEM_TEXT,
    ) -> CommandResult:
        """Executa argumentos de processo e normaliza falhas esperadas."""
        command = list(arguments)
        command_name = command[0]
        executable = shutil.which(command_name)
        if executable is None:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {command_name}",
                failure_kind=ProcessFailureKind.EXECUTABLE_MISSING,
            )
        command[0] = executable
        try:
            input_bytes = (
                input_text.encode("utf-8", errors="strict")
                if input_text is not None
                else None
            )
        except UnicodeEncodeError:
            return CommandResult(
                returncode=None,
                error="Falha ao codificar entrada textual como UTF-8",
                failure_kind=ProcessFailureKind.ENCODING_ERROR,
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
            completed = run_captured(command, **options)
        except FileNotFoundError:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {command_name}",
                failure_kind=ProcessFailureKind.EXECUTABLE_MISSING,
            )
        except subprocess.TimeoutExpired as error:
            return self._result(None, error.stdout or b"", error.stderr or b"",
                                stdout_policy, stderr_policy,
                                f"Comando excedeu o timeout de {self.timeout:g}s",
                                ProcessFailureKind.PROCESS_CLEANUP_ERROR if isinstance(error, ProcessCleanupError)
                                else ProcessFailureKind.TIMEOUT)
        except OSError as error:
            return CommandResult(None, error=f"Falha local de processo (errno={error.errno})",
                                 failure_kind=ProcessFailureKind.OS_ERROR)

        return self._result(completed.returncode, completed.stdout, completed.stderr,
                            stdout_policy, stderr_policy)

    def _result(self, returncode, stdout_bytes, stderr_bytes, stdout_policy,
                stderr_policy, error=None, failure_kind=None) -> CommandResult:
        values = {}
        invalid = []
        for stream, data, policy in (("stdout", stdout_bytes, stdout_policy),
                                     ("stderr", stderr_bytes, stderr_policy)):
            try:
                values[stream] = decode_output(data, policy, self.system_encoding)
            except UnicodeDecodeError:
                values[stream] = ""
                invalid.append(stream)
        if invalid:
            detail = "Falha ao decodificar " + ", ".join(invalid) + " como UTF-8"
            error = f"{error}; {detail}" if error else detail
            failure_kind = failure_kind or ProcessFailureKind.ENCODING_ERROR
        return CommandResult(returncode, **values, error=error, failure_kind=failure_kind,
                             stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
                             encoding_errors=tuple(invalid))
