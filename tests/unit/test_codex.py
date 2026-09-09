"""Testes unitários do adapter headless do Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.codex import (
    CODEX_TIMEOUT_SECONDS,
    CodexAdapter,
    CodexError,
    CodexProviderFailure,
)
from ai_dev_orchestrator.domain.provider import ProviderFailureKind
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner, ProcessFailureKind


@dataclass
class FakeRunner:
    result: CommandResult
    arguments: list[str] = field(default_factory=list)
    input_text: str | None = None

    def run(self, arguments: list[str], input_text: str | None = None, **policies) -> CommandResult:
        self.arguments = arguments
        self.input_text = input_text
        return self.result


def jsonl(session_id: str = "thread-123", message: str = "Implementação concluída") -> str:
    return "\n".join(
        [
            '{"type":"thread.started","thread_id":"' + session_id + '"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"'
            + message
            + '"}}',
            '{"type":"turn.completed"}',
        ]
    )


def test_executes_headless_in_explicit_worktree_and_captures_session(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    prompt = "Corrija $este valor; não execute via shell"
    runner = FakeRunner(CommandResult(0, jsonl(), "diagnóstico"))

    execution = CodexAdapter(runner).execute(worktree, prompt)

    assert execution.session_id == "thread-123"
    assert execution.final_message == "Implementação concluída"
    assert execution.stdout == jsonl()
    assert execution.stderr == "diagnóstico"
    assert execution.succeeded is True
    assert runner.arguments == ["codex", "exec", "-C", str(worktree.resolve()), "--json", "-"]
    assert runner.input_text == prompt
    assert prompt not in runner.arguments
    assert "--last" not in runner.arguments


def test_resumes_explicit_session_in_explicit_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = FakeRunner(CommandResult(0, jsonl("thread-123", "Correção concluída")))

    execution = CodexAdapter(runner).resume(worktree, "thread-123", "Aplique a correção")

    assert execution.session_id == "thread-123"
    assert execution.final_message == "Correção concluída"
    assert runner.arguments == [
        "codex", "exec", "-C", str(worktree.resolve()), "--json", "resume", "thread-123", "-",
    ]
    assert runner.input_text == "Aplique a correção"
    assert runner.input_text not in runner.arguments
    assert "--last" not in runner.arguments


def test_sends_large_prompt_only_through_stdin_without_truncation(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    prompt = "instrução extensa\n" + ("x" * 100_000)
    runner = FakeRunner(CommandResult(0, jsonl()))

    CodexAdapter(runner).execute(worktree, prompt)

    assert runner.input_text == prompt
    assert len(runner.input_text) > 100_000
    assert prompt not in runner.arguments
    assert runner.arguments == ["codex", "exec", "-C", str(worktree.resolve()), "--json", "-"]


def test_resume_rejects_jsonl_without_returned_thread_event(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = FakeRunner(
        CommandResult(
            0,
            "\n".join(
                [
                    '{"type":"item.completed","item":{"type":"agent_message","text":"Ok"}}',
                    '{"type":"turn.completed"}',
                ]
            ),
        )
    )

    with pytest.raises(CodexError, match="sem retornar o identificador"):
        CodexAdapter(runner).resume(worktree, "thread-123", "Continue")


def test_rejects_empty_session_id_before_running_codex(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = FakeRunner(CommandResult(0, jsonl()))

    with pytest.raises(CodexError, match="obrigatório"):
        CodexAdapter(runner).resume(worktree, "  ", "Continue")

    assert runner.arguments == []


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(None, error="Executável não encontrado: codex", failure_kind=ProcessFailureKind.EXECUTABLE_MISSING), "não encontrado"),
        (CommandResult(None, error="Comando excedeu o timeout de 1800s", failure_kind=ProcessFailureKind.TIMEOUT), "timeout"),
        (CommandResult(1, stderr="sessão não encontrada"), "código 1.*saída omitida"),
    ],
)
def test_reports_process_failures(tmp_path: Path, result: CommandResult, message: str) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(CodexError, match=message):
        CodexAdapter(FakeRunner(result)).execute(worktree, "Implemente")


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("não é json", "JSONL inválido"),
        ("", "não retornou eventos JSONL"),
        ('{"type":"item.completed","item":{"type":"agent_message","text":"Ok"}}', "sem retornar o identificador"),
        ('{"type":"thread.started","thread_id":"thread-123"}', "sem retornar a mensagem final"),
        (
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-123"}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"Ok"}}',
                ]
            ),
            "evento de conclusão",
        ),
    ],
)
def test_rejects_invalid_or_incomplete_jsonl(tmp_path: Path, output: str, message: str) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(CodexError, match=message):
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")


def test_rejects_session_returned_by_resume_that_differs_from_requested(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(CodexError, match="diferente da solicitada"):
        CodexAdapter(FakeRunner(CommandResult(0, jsonl("thread-outra")))).resume(
            worktree, "thread-123", "Continue"
        )


def test_reports_failure_when_resuming_session(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(CodexError, match="retomar a sessão.*saída omitida"):
        CodexAdapter(FakeRunner(CommandResult(1, stderr="sessão não encontrada"))).resume(
            worktree, "thread-123", "Continue"
        )


def test_rejects_invalid_worktree_without_running_codex(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(0, jsonl()))

    with pytest.raises(CodexError, match="worktree informado"):
        CodexAdapter(runner).execute(tmp_path / "ausente", "Implemente")

    assert runner.arguments == []


def test_uses_provider_timeout_without_changing_default_process_timeout() -> None:
    adapter = CodexAdapter()

    assert isinstance(adapter.runner, CommandRunner)
    assert adapter.runner.timeout == CODEX_TIMEOUT_SECONDS
    assert CommandRunner().timeout == 5
    assert CODEX_TIMEOUT_SECONDS > 5


def test_accepts_injected_timeout_for_default_runner() -> None:
    adapter = CodexAdapter(timeout=12)

    assert isinstance(adapter.runner, CommandRunner)
    assert adapter.runner.timeout == 12


def test_accepts_intermediate_error_before_completed_turn(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"turn.started"}',
            '{"type":"error","code":"network","message":"network error token=secret"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Concluído"}}',
            '{"type":"turn.completed"}',
        ]
    )

    execution = CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert execution.succeeded is True
    assert execution.final_message == "Concluído"


def test_accepts_nested_intermediate_error_before_completed_turn(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"item.completed","error":{"code":"network","message":"network error token=secret"},"item":{"type":"agent_message","text":"Concluído"}}',
            '{"type":"turn.completed"}',
        ]
    )

    execution = CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert execution.succeeded is True


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ('{"code":"quota_exceeded","message":"quota exceeded"}', ProviderFailureKind.TERMINAL_QUOTA),
        ('{"code":"network","message":"network error"}', ProviderFailureKind.NETWORK_ERROR),
    ],
)
def test_turn_failed_is_terminal_even_with_exit_zero(tmp_path: Path, error: str,
                                                     kind: ProviderFailureKind) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"error","error":' + error + '}',
            '{"type":"turn.failed","error":' + error + '}',
        ]
    )

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert raised.value.classification == kind


def test_error_without_terminal_turn_preserves_structured_classification(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"error","code":"quota_exceeded","message":"token=secret"}',
        ]
    )

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert raised.value.classification == ProviderFailureKind.TERMINAL_QUOTA
    assert "secret" not in raised.value.message


def test_incompatible_terminal_events_are_protocol_error(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"turn.completed"}',
            '{"type":"turn.failed","error":{"message":"token=secret"}}',
        ]
    )

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert raised.value.classification == ProviderFailureKind.PROTOCOL_ERROR
    assert raised.value.diagnostic_context == (
        "events=thread.started,turn.completed,turn.failed; terminal=ambiguous; count=3; source=JSONL; exit=0"
    )
    assert "secret" not in raised.value.message


def test_nonzero_exit_still_uses_structured_failure(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = '{"type":"error","code":"quota_exceeded","message":"quota exceeded"}'

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(1, output))).execute(worktree, "Implemente")

    assert raised.value.classification == ProviderFailureKind.TERMINAL_QUOTA


@pytest.mark.parametrize(
    "output",
    [
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-123"}',
                '{"type":"turn.completed"}',
            ]
        ),
        "\n".join(
            [
                '{"type":"item.completed","item":{"type":"agent_message","text":"Ok"}}',
                '{"type":"turn.completed"}',
            ]
        ),
    ],
)
def test_completed_turn_requires_message_and_session(tmp_path: Path, output: str) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert raised.value.classification == ProviderFailureKind.PROTOCOL_ERROR


def test_unknown_diagnostic_contains_only_sanitized_protocol_metadata(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    output = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"turn.failed","error":{"message":"prompt secreto token=abc"}}',
        ]
    )

    with pytest.raises(CodexProviderFailure) as raised:
        CodexAdapter(FakeRunner(CommandResult(0, output))).execute(worktree, "Implemente")

    assert raised.value.classification == ProviderFailureKind.UNKNOWN
    assert raised.value.diagnostic_context == (
        "events=thread.started,turn.failed; terminal=turn.failed; count=2; source=JSONL; exit=0"
    )
    assert "prompt secreto" not in raised.value.message
    assert "token=abc" not in raised.value.message
