"""Fixtures de incidentes e precedência independente da ordem dos eventos."""

from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ai_dev_orchestrator.adapters.codex import CodexAdapter
from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind as Kind
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner, OutputPolicy


FIXTURES = Path(__file__).parents[1] / "fixtures"


class Runner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, arguments, input_text=None, **policies):
        assert policies["stdout_policy"] == OutputPolicy.UTF8_STRICT
        self.calls.append((arguments, input_text))
        return self.result


def jsonl(*events):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


@pytest.mark.parametrize("operation", ["execute", "resume"])
def test_fixture_real_quota(operation, tmp_path):
    fixture = json.loads((FIXTURES / "codex" / f"quota-{operation}.json").read_text(encoding="utf-8"))
    runner = Runner(CommandResult(fixture["returncode"], jsonl(*fixture["events"]), fixture["stderr"]))
    adapter = CodexAdapter(runner)
    with pytest.raises(ProviderFailure) as caught:
        if operation == "execute":
            adapter.execute(tmp_path, "implemente")
        else:
            adapter.resume(tmp_path, fixture["session_id"], "corrija")
    failure = caught.value
    assert failure.classification == Kind.TERMINAL_QUOTA
    assert failure.returncode == 126 and failure.retry_at is None
    assert failure.session_id == fixture["events"][0]["thread_id"]
    assert "5:09" not in str(failure)
    assert len(runner.calls) == 1
    assert ("resume" in runner.calls[0][0]) == (operation == "resume")


@pytest.mark.parametrize("message,kind", [
    ("You've hit your usage limit", Kind.TERMINAL_QUOTA),
    ("quota exceeded", Kind.TERMINAL_QUOTA),
    ("HTTP 429 too many requests", Kind.TRANSIENT_RATE_LIMIT),
    ("authentication failed", Kind.AUTH_ERROR),
    ("model not found", Kind.MODEL_UNAVAILABLE),
    ("network error: connection reset", Kind.NETWORK_ERROR),
    ("request timed out", Kind.TIMEOUT),
    ("mensagem arbitrária", Kind.UNKNOWN),
])
@pytest.mark.parametrize("event_type", ["error", "turn.failed"])
@pytest.mark.parametrize("exit_code", [0, 126])
def test_mensagens_de_erro_nunca_sao_sucesso(tmp_path, message, kind, event_type, exit_code):
    error = {"message": message}
    event = {"type": event_type, **error} if event_type == "error" else {"type": event_type, "error": error}
    output = jsonl({"type": "thread.started", "thread_id": "same"}, event)
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(exit_code, output))).resume(tmp_path, "same", "p")
    assert caught.value.classification == kind and caught.value.session_id == "same"


@pytest.mark.parametrize("code,kind", [
    ("quota_exceeded", Kind.TERMINAL_QUOTA), ("rate_limit", Kind.TRANSIENT_RATE_LIMIT),
    ("network", Kind.NETWORK_ERROR), ("authentication", Kind.AUTH_ERROR),
    ("model_unavailable", Kind.MODEL_UNAVAILABLE), ("inexistente", Kind.UNKNOWN),
])
def test_erro_estruturado_legado(tmp_path, code, kind):
    output = jsonl({"type": "thread.started", "thread_id": "same"},
                   {"type": "turn.failed", "error": {"code": code, "retry_at": "2026-09-08T05:09:00-03:00"}})
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(1, output))).execute(tmp_path, "p")
    assert caught.value.classification == kind
    assert caught.value.retry_at == datetime(2026, 9, 8, 8, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize("events", list(itertools.permutations([
    {"type": "turn.failed", "error": {"code": "network"}},
    {"type": "error", "message": "HTTP 429 rate limit"},
    {"type": "error", "message": "You've hit your usage limit"},
])))
def test_quota_prevalece_independente_da_ordem(tmp_path, events):
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(126, jsonl(*events), "network error"))).resume(tmp_path, "same", "p")
    assert caught.value.classification == Kind.TERMINAL_QUOTA
    assert caught.value.session_id == "same"


@pytest.mark.parametrize("retry,expected", [
    ("2026-09-08T05:09:00-03:00", datetime(2026, 9, 8, 8, 9, tzinfo=timezone.utc)),
    ("2026-09-08T08:09:00Z", datetime(2026, 9, 8, 8, 9, tzinfo=timezone.utc)),
    ("5:09 AM", None), ("tomorrow at 5:09 AM", None),
    ("2026-09-08T05:09:00", None), ("2026-99-99T05:09:00Z", None),
])
def test_retry_textual_exige_data_e_fuso(tmp_path, retry, expected):
    output = jsonl({"type": "error", "message": f"You've hit your usage limit. try again at {retry}."})
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(126, output))).resume(tmp_path, "same", "p")
    assert caught.value.retry_at == expected


def test_retry_conflitante_nao_inventa_janela(tmp_path):
    output = jsonl(*({"type": "error", "error": {"code": "quota_exceeded", "retry_at": r}}
                     for r in ["2026-09-08T05:09:00Z", "2026-09-09T05:09:00Z"]))
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(126, output))).resume(tmp_path, "same", "p")
    assert caught.value.retry_at is None


@pytest.mark.parametrize("output,kind", [
    ('{"type":"thread.started","thread_id":"same"}\n{quebrado', Kind.MALFORMED_JSON),
    ('{"type":"thread.started","thread_id":"same"}', Kind.PROTOCOL_ERROR),
    ('{"type":"thread.started","thread_id":"other"}', Kind.PROTOCOL_ERROR),
    ('{"type":"error","message":"texto secreto token=segredo https://secret.invalid/x"}', Kind.UNKNOWN),
])
def test_falha_preserva_sessao_e_omite_dados_brutos(tmp_path, output, kind):
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(0, output))).resume(tmp_path, "same", "prompt secreto")
    assert caught.value.classification == kind and caught.value.session_id == "same"
    assert "segredo" not in str(caught.value) and "https://" not in str(caught.value)


def test_mensagem_do_agente_nao_e_sinal_de_quota(tmp_path):
    output = jsonl({"type": "thread.started", "thread_id": "same"},
                   {"type": "item.completed", "item": {"type": "agent_message", "text": "Teste de usage limit"}},
                   {"type": "turn.completed"})
    assert CodexAdapter(Runner(CommandResult(0, output))).execute(tmp_path, "p").succeeded


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_provider_nao_aceita_cp1252_como_json(monkeypatch, tmp_path, provider):
    monkeypatch.setattr(shutil, "which", lambda name: name)
    def run(args, **kwargs):
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, b"1.1.27", b"")
        if args[-1] == "--help":
            return subprocess.CompletedProcess(args, 0, (FIXTURES / "antigravity/help-1.1.27.txt").read_bytes(), b"")
        return subprocess.CompletedProcess(args, 126, b'{"text":"\xe7"}', b"")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)
    runner = CommandRunner(system_encoding="cp1252")
    with pytest.raises(ProviderFailure) as caught:
        if provider == "codex":
            CodexAdapter(runner).resume(tmp_path, "same", "p")
        else:
            AntigravityAdapter(10, runner).invoke("p", tmp_path, {})
    assert caught.value.classification == Kind.ENCODING_ERROR
    assert caught.value.returncode == 126


def test_timeout_parcial_codex_nao_perde_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: name)
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1,
            output=b'{"type":"thread.started","thread_id":"partial"}\n', stderr=b"")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(CommandRunner(timeout=1)).execute(tmp_path, "p")
    assert caught.value.classification == Kind.TIMEOUT and caught.value.session_id == "partial"


@pytest.mark.parametrize("timeout", [False, True])
def test_prefixo_utf8_preserva_id_sem_aceitar_linha_corrompida(monkeypatch, tmp_path, timeout):
    monkeypatch.setattr(shutil, "which", lambda name: name)
    output = b'{"type":"thread.started","thread_id":"partial"}\n{"type":"error","message":"\xe7"}\n'
    def run(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(args[0], 1, output=output, stderr=b"")
        return subprocess.CompletedProcess(args[0], 126, output, b"")
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.run_captured", run)
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(CommandRunner(system_encoding="cp1252")).execute(tmp_path, "p")
    assert caught.value.classification == (Kind.TIMEOUT if timeout else Kind.ENCODING_ERROR)
    assert caught.value.session_id == "partial"
    assert caught.value.returncode == (None if timeout else 126)


@pytest.mark.parametrize("value", [[], {}, 42, "https://secret.invalid/session", ""])
def test_thread_id_invalido_falha_fechado(tmp_path, value):
    with pytest.raises(ProviderFailure) as caught:
        CodexAdapter(Runner(CommandResult(126, jsonl({"type": "thread.started", "thread_id": value})))).execute(tmp_path, "p")
    assert caught.value.classification == Kind.PROTOCOL_ERROR
    assert caught.value.session_id is None
