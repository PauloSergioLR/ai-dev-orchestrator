"""Regressões Windows/Linux da fronteira de bytes, texto humano e protocolo."""

import os
import shutil
import subprocess
import sys
import time

import pytest

from ai_dev_orchestrator.infrastructure.process import (
    CommandRunner, OutputPolicy, ProcessFailureKind,
)
from ai_dev_orchestrator.services.validation import LocalValidationService
from ai_dev_orchestrator.domain.project_contract import CommandPlan


def process(monkeypatch, stdout, stderr=b"", code=0):
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", lambda *a, **kw: subprocess.CompletedProcess(a[0], code, stdout, stderr))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_texto_acentuado_por_stream(monkeypatch, stream, encoding):
    data = "validação ç ã é".encode(encoding)
    process(monkeypatch, data if stream == "stdout" else b"", data if stream == "stderr" else b"")
    result = CommandRunner(system_encoding="cp1252").run(["pytest"])
    assert result.succeeded
    assert getattr(result, stream) == "validação ç ã é"
    assert getattr(result, stream + "_bytes") == data


@pytest.mark.parametrize("code", [0, 2, 126])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_protocolo_estrito_preserva_exit_e_outro_stream(monkeypatch, code, stream):
    process(monkeypatch, b'{"text":"\xe7"}' if stream == "stdout" else b"ok",
            b'\xe7' if stream == "stderr" else b"ok", code)
    result = CommandRunner(system_encoding="cp1252").run(["tool"], **{stream + "_policy": OutputPolicy.UTF8_STRICT})
    assert result.returncode == code
    assert not result.succeeded
    assert result.failure_kind == ProcessFailureKind.ENCODING_ERROR
    assert result.encoding_errors == (stream,)
    assert getattr(result, stream) == ""
    assert getattr(result, "stderr" if stream == "stdout" else "stdout") == "ok"


def test_json_utf8_com_stderr_cp1252(monkeypatch):
    process(monkeypatch, '{"text":"ç"}'.encode(), b"diagn\xf3stico")
    result = CommandRunner(system_encoding="cp1252").run(["codex"], stdout_policy=OutputPolicy.UTF8_STRICT)
    assert result.succeeded and result.stderr == "diagnóstico"
    assert result.stdout == '{"text":"ç"}'


def test_binario_nao_tenta_decodificar(monkeypatch):
    process(monkeypatch, b"\xff\x00\x81", b"\xe7")
    result = CommandRunner().run(["tool"], stdout_policy=OutputPolicy.BINARY, stderr_policy=OutputPolicy.BINARY)
    assert result.succeeded and result.stdout == "" and result.stderr == ""
    assert result.stdout_bytes == b"\xff\x00\x81" and result.stderr_bytes == b"\xe7"


def test_texto_sem_codepage_compativel_preserva_byte_visivel(monkeypatch):
    process(monkeypatch, b"\xe7")
    result = CommandRunner(system_encoding="utf-8").run(["pytest"])
    assert result.succeeded and result.stdout == "\\xe7"


def test_gate_pytest_com_byte_real_e7(monkeypatch, tmp_path):
    process(monkeypatch, b"valida\xe7\xe3o: 12 passed")
    plans = (CommandPlan("tests", "unit", "Tests", ("pytest",)),)
    gates = LocalValidationService(CommandRunner(system_encoding="cp1252"), plans).validate(tmp_path)
    assert len(gates) == 1
    assert gates[0].name == "tests" and gates[0].succeeded
    assert gates[0].diagnostic == "validação: 12 passed"


def test_subprocesso_real_stdin_grande_caminho_acentuado(tmp_path):
    worktree = tmp_path / "diretório com espaços"
    worktree.mkdir()
    script = worktree / "eco.py"
    script.write_text("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.stderr.buffer.write('ç'.encode('cp1252'))\n", encoding="utf-8")
    prompt = "Correção ç 漢字\n" * 20000
    result = CommandRunner(timeout=10, system_encoding="cp1252").run(
        [sys.executable, str(script)], cwd=worktree, input_text=prompt,
        stdout_policy=OutputPolicy.UTF8_STRICT,
    )
    assert result.succeeded and result.stdout == prompt and result.stderr == "ç"
    if os.name == "nt":
        assert sys.executable.lower().endswith(".exe")


def test_timeout_real_preserva_saida_parcial(tmp_path):
    script = tmp_path / "timeout.py"
    script.write_text("import sys,time\nprint('parcial', flush=True)\ntime.sleep(20)\n", encoding="utf-8")
    result = CommandRunner(timeout=1).run([sys.executable, str(script)])
    assert result.returncode is None and result.failure_kind == ProcessFailureKind.TIMEOUT
    assert result.stdout.strip() == "parcial"


def test_timeout_real_encerra_descendente_antes_de_retry(tmp_path):
    worktree = tmp_path / "árvore com espaços"
    worktree.mkdir()
    marker = worktree / "nao-deve-existir.txt"
    child = worktree / "filho.py"
    child.write_text("import time,pathlib,sys\ntime.sleep(3)\npathlib.Path(sys.argv[1]).write_text('vivo')\n", encoding="utf-8")
    parent = worktree / "pai.py"
    parent.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\nprint('iniciado',flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    result = CommandRunner(timeout=1).run([sys.executable, str(parent), str(child), str(marker)])
    assert result.failure_kind == ProcessFailureKind.TIMEOUT
    time.sleep(3)
    assert not marker.exists()


def test_limpeza_nao_comprovada_nunca_vira_timeout_retentavel(monkeypatch):
    """ProcessCleanupError → PROCESS_CLEANUP_ERROR (INTERVENTION), nunca TIMEOUT (RETRY)."""
    from ai_dev_orchestrator.infrastructure.process import ProcessCleanupError
    from ai_dev_orchestrator.domain.provider import (
        FAILURE_POLICY, FailureDisposition, classify_process_failure,
    )

    monkeypatch.setattr(shutil, "which", lambda name: name)

    def run(*args, **kwargs):
        raise ProcessCleanupError(args[0], 1, output=b"parcial", stderr=b"")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)

    result = CommandRunner(timeout=1).run([sys.executable, "noop.py"])
    assert result.failure_kind == ProcessFailureKind.PROCESS_CLEANUP_ERROR
    assert result.failure_kind != ProcessFailureKind.TIMEOUT

    kind = classify_process_failure(result.failure_kind)
    assert kind.value == "PROCESS_CLEANUP_ERROR"
    assert FAILURE_POLICY[kind] == FailureDisposition.INTERVENTION



@pytest.mark.parametrize("code", [0, 2, 126])
def test_processo_real_preserva_exit_code(code):
    result = CommandRunner(timeout=10).run([sys.executable, "-c", f"raise SystemExit({code})"])
    assert result.returncode == code
    assert result.succeeded == (code == 0)


@pytest.mark.skipif(os.name != "nt", reason="Job Object é específico do Windows")
def test_falha_ao_vincular_job_nao_inicia_comando(monkeypatch, tmp_path):
    from ai_dev_orchestrator.infrastructure.windows_job import WindowsJob

    marker = tmp_path / "nao-iniciar.txt"

    def deny(self, process):
        raise PermissionError("simulação de vínculo recusado")

    monkeypatch.setattr(WindowsJob, "assign", deny)
    result = CommandRunner(timeout=10).run([
        sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker),
    ])
    assert result.failure_kind == ProcessFailureKind.OS_ERROR
    assert not marker.exists()
