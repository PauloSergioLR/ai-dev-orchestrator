"""Execução segura e reutilizável de processos locais."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import sys
import locale
from math import isfinite
import os
from pathlib import Path
import shutil
import signal
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from typing import Mapping, Sequence


COMMAND_TIMEOUT_SECONDS = 5
MAX_CAPTURE_BYTES = 16 * 1024 * 1024


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
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    UNSAFE_COMMAND = "UNSAFE_COMMAND"


class ProcessCleanupError(subprocess.TimeoutExpired):
    """Timeout cuja árvore de processos não pôde ser encerrada com segurança."""


class ProcessIdleTimeout(subprocess.TimeoutExpired):
    """Processo sem atividade de saída durante a janela configurada."""


class ProcessOutputLimit(subprocess.TimeoutExpired):
    """Captura interrompida porque excedeu o teto de memória autorizado."""


@dataclass
class ProcessActivity:
    """Metadados de saída observada, sem armazenar conteúdo do provider."""

    output_bytes: int = 0
    last_output_at: float = field(default_factory=monotonic)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, count: int) -> None:
        with self._lock:
            self.output_bytes += count
            self.last_output_at = monotonic()

    def snapshot(self) -> tuple[int, float]:
        with self._lock:
            return self.output_bytes, max(0, monotonic() - self.last_output_at)


def run_captured(
    command, *, capture_output, timeout, shell, check, cwd=None, input=None, env=None,
    idle_timeout=None, activity=None,
):
    """Captura bytes e encerra a árvore antes de permitir retry após timeout."""
    if shell or not capture_output or check:
        raise ValueError("Contrato de processo exige captura, shell=False e check=False")
    job = None
    lifeline_read = lifeline_write = None
    launch_command = command
    options = {"start_new_session": True}
    if os.name == "nt":
        from ai_dev_orchestrator.infrastructure.windows_job import WindowsJob
        job = WindowsJob()
        launch_command = [sys.executable, "-I", "-S", str(Path(__file__).with_name("_process_child.py"))]
        header = json.dumps({"command": command, "has_input": input is not None}).encode("ascii")
        input = header + b"\n" + (input or b"")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        # EOF neste pipe prova a morte do orquestrador, inclusive SIGKILL.
        lifeline_read, lifeline_write = os.pipe()
        launch_command = [sys.executable, "-I", "-S", str(Path(__file__).with_name("_process_child.py")), str(lifeline_read)]
        header = json.dumps({"command": command, "has_input": input is not None}).encode("ascii")
        input = header + b"\n" + (input or b"")
        options["pass_fds"] = (lifeline_read,)
    if env is not None:
        options["env"] = env
    try:
        process = subprocess.Popen(launch_command, cwd=cwd, shell=False,
                                   stdin=subprocess.PIPE if input is not None else None,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        if lifeline_read is not None:
            os.close(lifeline_read)
            lifeline_read = None
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
        for descriptor in (lifeline_read, lifeline_write):
            if descriptor is not None:
                os.close(descriptor)
        raise
    try:
        stdout, stderr = _communicate_monitored(
            process, input, timeout, idle_timeout, activity or ProcessActivity(), job,
        )
        if job is None and not _stop_process_tree(process):
            raise ProcessCleanupError(command, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if job is not None:
            job.close()
        if lifeline_write is not None:
            os.close(lifeline_write)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _communicate_monitored(process, input_bytes, timeout, idle_timeout, activity, job):
    """Drena ambos os pipes simultaneamente e limita tempo total e silêncio."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    failures: list[OSError] = []
    changed = Event()
    overflow = Event()
    buffer_lock = Lock()

    def read(stream, name):
        try:
            while chunk := stream.read1(65536):
                with buffer_lock:
                    remaining = MAX_CAPTURE_BYTES - sum(len(value) for value in buffers.values())
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                activity.record(len(chunk))
                changed.set()
        except OSError as error:
            failures.append(error)

    def write():
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except OSError as error:
            failures.append(error)

    threads = [Thread(target=read, args=(getattr(process, name), name), daemon=True)
               for name in buffers]
    if input_bytes is not None:
        threads.append(Thread(target=write, daemon=True))
    started = monotonic()
    for thread in threads:
        thread.start()
    try:
        while True:
            if overflow.is_set():
                raise ProcessOutputLimit(process.args, timeout)
            if process.poll() is not None and not any(thread.is_alive() for thread in threads):
                break
            if monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(process.args, timeout)
            if idle_timeout is not None and activity.snapshot()[1] >= idle_timeout:
                raise ProcessIdleTimeout(process.args, idle_timeout)
            changed.wait(0.05)
            changed.clear()
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        stopped = job.stop() if job is not None else _stop_process_tree(process)
        deadline = monotonic() + 5
        for thread in threads:
            thread.join(max(0, deadline - monotonic()))
        stopped = stopped and not any(thread.is_alive() for thread in threads)
        if isinstance(error, KeyboardInterrupt) and stopped:
            raise
        failure = type(error) if stopped and isinstance(error, subprocess.TimeoutExpired) else subprocess.TimeoutExpired if stopped else ProcessCleanupError
        raise failure(process.args, getattr(error, "timeout", timeout),
                      output=bytes(buffers["stdout"]), stderr=bytes(buffers["stderr"])) from error
    if failures:
        raise failures[0]
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def resolve_executable(command_name: str, cwd=None, environment=None) -> str | None:
    """Resolve caminhos relativos e PATH no mesmo contexto usado pelo filho."""
    if cwd is None and environment is None:
        return shutil.which(command_name)
    root = Path(cwd or Path.cwd()).resolve()
    effective = os.environ if environment is None else environment
    lookup = {key.upper() if os.name == "nt" else key: value for key, value in effective.items()}
    if os.path.dirname(command_name):
        candidates = [Path(command_name) if Path(command_name).is_absolute() else root / command_name]
    else:
        search_path = lookup.get("PATH", os.defpath)
        candidates = [(root / part / command_name) for part in search_path.split(os.pathsep)] if search_path else []
    extensions = [""]
    if os.name == "nt":
        extensions.extend(part for part in lookup.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if part)
    for candidate in candidates:
        for extension in extensions:
            target = Path(str(candidate) + extension)
            if target.is_file() and os.access(target, os.F_OK | os.X_OK):
                return str(target.resolve())
    return None


def _unsafe_windows_batch(command: Sequence[str]) -> bool:
    # shell=False não impede CreateProcess/cmd de interpretar scripts batch.
    # Recusar caracteres de expansão evita executar uma segunda instrução.
    return os.name == "nt" and Path(command[0]).suffix.casefold() in {".cmd", ".bat"} and any(
        any(character in argument for character in '&|<>^%!"\r\n()')
        for argument in command
    )


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
                 system_encoding: str | None = None,
                 idle_timeout: float | None = None) -> None:
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout deve ser positivo e finito")
        if idle_timeout is not None and (not isfinite(idle_timeout) or idle_timeout <= 0):
            raise ValueError("idle_timeout deve ser positivo e finito")
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.activity = ProcessActivity()
        self.system_encoding = system_encoding or locale.getencoding()

    def run(
        self,
        arguments: Sequence[str],
        cwd: str | Path | None = None,
        input_text: str | None = None,
        *,
        stdout_policy: OutputPolicy = OutputPolicy.SYSTEM_TEXT,
        stderr_policy: OutputPolicy = OutputPolicy.SYSTEM_TEXT,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Executa argumentos de processo e normaliza falhas esperadas."""
        command = list(arguments)
        if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
            raise ValueError("Comando deve conter argv textual válido")
        self.activity = ProcessActivity()
        command_name = command[0]
        executable = resolve_executable(command_name, cwd, environment)
        if executable is None:
            return CommandResult(
                returncode=None,
                error=f"Executável não encontrado: {command_name}",
                failure_kind=ProcessFailureKind.EXECUTABLE_MISSING,
            )
        command[0] = executable
        if _unsafe_windows_batch(command):
            return CommandResult(None, error="Argumentos batch exigem interpretação de shell; use um executável nativo",
                                 failure_kind=ProcessFailureKind.UNSAFE_COMMAND)
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
            if environment is not None:
                options["env"] = environment
            if self.idle_timeout is not None:
                options.update(idle_timeout=self.idle_timeout, activity=self.activity)
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
                                ("Saída do comando excedeu o limite de captura" if isinstance(error, ProcessOutputLimit)
                                 else f"Comando sem saída durante {self.idle_timeout:g}s" if isinstance(error, ProcessIdleTimeout)
                                 else f"Comando excedeu o timeout de {self.timeout:g}s"),
                                ProcessFailureKind.PROCESS_CLEANUP_ERROR if isinstance(error, ProcessCleanupError)
                                else ProcessFailureKind.OUTPUT_LIMIT if isinstance(error, ProcessOutputLimit)
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
