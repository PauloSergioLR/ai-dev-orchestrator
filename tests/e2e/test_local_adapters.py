"""E2E local dos contratos de processo que causaram incidentes reais."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexProviderFailure
from ai_dev_orchestrator.domain.ci import CiResult, CiStatus
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner, OutputPolicy
from ai_dev_orchestrator.services.pipeline import RunPipeline
from ai_dev_orchestrator.services.validation import LocalValidationError, LocalValidationService
from ai_dev_orchestrator.domain.project_contract import CommandPlan


class ScriptedRunner:
    """Roteiro declarativo de respostas de CLI, sem subprocesso ou rede."""

    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, arguments, cwd=None, input_text=None, **kwargs) -> CommandResult:
        self.calls.append((list(arguments), input_text))
        return self.responses.pop(0)


def ok_json(value: object) -> CommandResult:
    return CommandResult(0, stdout=json.dumps(value))


def test_codex_quota_textual_jsonl_e_network_error_sao_classificados_sem_nova_thread(tmp_path: Path) -> None:
    quota_jsonl = json.dumps({"type": "thread.started", "thread_id": "sessao-67"}) + "\n" + json.dumps({
        "type": "turn.failed", "error": {"message": "You've hit your usage limit"}
    })
    runner = ScriptedRunner([CommandResult(1, stdout=quota_jsonl)])
    adapter = CodexAdapter(runner=runner)

    failure = None
    try:
        adapter.execute(tmp_path, "prompt confidencial" * 100)
    except CodexProviderFailure as raised:
        failure = raised
    else:
        pytest.fail("quota deveria interromper a execução")

    assert failure is not None
    assert failure.classification == ProviderFailureKind.TERMINAL_QUOTA
    assert failure.session_id == "sessao-67"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("result, expected", [
    (CommandResult(None, error="timeout", failure_kind="TIMEOUT"), ProviderFailureKind.TIMEOUT),
    (CommandResult(None, error="missing", failure_kind="EXECUTABLE_MISSING"), ProviderFailureKind.EXECUTABLE_MISSING),
])
def test_codex_falhas_locais_sao_reproduziveis(result: CommandResult, expected, tmp_path: Path) -> None:
    adapter = CodexAdapter(runner=ScriptedRunner([result]))
    failure = None
    try:
        adapter.execute(tmp_path, "prompt")
    except CodexProviderFailure as raised:
        failure = raised
    else:
        pytest.fail("falha local deveria interromper a execução")
    assert failure is not None
    assert failure.classification == expected


@pytest.mark.parametrize("payload, expected", [
    ({"status": "SUCCESS", "structured_output": {"verdict": "APPROVED"}}, None),
    ({"status": "SUCCESS"}, ProviderFailureKind.PROTOCOL_ERROR),
    ("{inválido", ProviderFailureKind.MALFORMED_JSON),
    ({"status": "ERROR", "error": {"code": "RATE_LIMIT", "message": "rate limit"}}, ProviderFailureKind.TRANSIENT_RATE_LIMIT),
])
def test_antigravity_fail_closed_para_contrato_e_quota(payload, expected) -> None:
    help_text = "--input-format --sandbox --disable-slash-commands --print-timeout --output-format --json-schema"
    response = CommandResult(0, stdout=payload) if isinstance(payload, str) else ok_json(payload)
    runner = ScriptedRunner([CommandResult(0, stdout="1.1"), CommandResult(0, stdout=help_text), response])
    adapter = AntigravityAdapter(1, runner=runner)

    if expected is None:
        assert json.loads(adapter.invoke("prompt realista " * 2_000, ".", {})) == {"verdict": "APPROVED"}
        assert len(runner.calls[-1][1] or "") > 10_000
    else:
        failure = None
        try:
            adapter.invoke("prompt", ".", {})
        except ProviderFailure as raised:
            failure = raised
        else:
            pytest.fail("resposta inválida do reviewer deveria falhar fechada")
        assert failure is not None
        assert failure.classification == expected


class ReviewReader:
    def __init__(self, head: str) -> None:
        self.head = head

    def get_review_data(self, number: int):
        return {
            "number": number, "url": "https://example.test/pr/1", "baseRefName": "main",
            "headRefName": "work/e2e", "headRefOid": self.head, "commits": [self.head],
            "files": ["src/process.py"], "diff": "diff --git a/x b/x",
        }


class TwoStageReviewer:
    def __init__(self, failure_at: int) -> None:
        self.failure_at = failure_at
        self.calls: list[dict] = []

    def invoke(self, prompt: str, cwd, schema: dict) -> str:
        self.calls.append(schema)
        if len(self.calls) == self.failure_at:
            raise ProviderFailure("gemini", ProviderFailureKind.NETWORK_ERROR,
                                  "falha roteirizada", datetime.now(timezone.utc))
        return json.dumps({field: [] for field in schema["required"]})


@pytest.mark.parametrize("failure_at", [1, 2])
def test_falha_em_cada_etapa_do_review_nao_libera_review_ou_merge(tmp_path: Path, failure_at: int) -> None:
    """O plano e o StructuredReview são chamadas distintas e ambas são fail-closed."""
    head = "a" * 40
    reviewer = TwoStageReviewer(failure_at)
    pipeline = object.__new__(RunPipeline)
    pipeline.review_reader = ReviewReader(head)
    pipeline.reviewer = reviewer
    pipeline.config = SimpleNamespace(review=SimpleNamespace(blocking_severities=("CRITICAL", "HIGH", "MEDIUM")))
    worktree = GitWorktree(tmp_path, tmp_path, "work/e2e", "main")
    issue = Issue(67, "review e2e", "body", "OPEN", "url", (), ())
    pull_request = PullRequest(1, "https://example.test/pr/1", "PR", "main", "work/e2e")
    ci = CiResult(head, (), CiStatus.SUCCESS)

    failure = None
    try:
        pipeline._review_head(issue, worktree, pull_request, head, (), ci, ())
    except ProviderFailure as raised:
        failure = raised
    else:
        pytest.fail("falha do reviewer não pode produzir aprovação")

    assert failure is not None
    assert len(reviewer.calls) == failure_at
    assert reviewer.calls[0].get("required") != reviewer.calls[-1].get("required") or failure_at == 1


def test_gates_locais_preservam_return_code_mesmo_com_saida_cp1252(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.buffer.write('ação'.encode('cp1252')); sys.exit(7)"
    runner = CommandRunner(system_encoding="cp1252")
    result = runner.run((sys.executable, "-c", script), stdout_policy=OutputPolicy.SYSTEM_TEXT)
    assert result.returncode == 7
    assert result.succeeded is False
    assert "ação" in result.stdout

    class GateRunner:
        def run(self, arguments, cwd=None):
            return result

    with pytest.raises(LocalValidationError, match="quality"):
        LocalValidationService(
            GateRunner(), (CommandPlan("quality", "lint", "Quality", ("tool",)),)
        ).validate(tmp_path)


def test_decoding_estrito_nao_mascara_return_code(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.buffer.write(b'\\x81'); sys.exit(9)"
    result = CommandRunner(system_encoding="cp1252").run(
        (sys.executable, "-c", script), stdout_policy=OutputPolicy.UTF8_STRICT
    )
    assert result.returncode == 9
    assert result.failure_kind == "ENCODING_ERROR"
    assert result.succeeded is False
