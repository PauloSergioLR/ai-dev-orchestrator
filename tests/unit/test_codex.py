"""Testes unitários do adapter headless do Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.codex import (
    CODEX_TIMEOUT_SECONDS,
    CodexAdapter,
    CodexError,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


@dataclass
class FakeRunner:
    result: CommandResult
    arguments: list[str] = field(default_factory=list)

    def run(self, arguments: list[str]) -> CommandResult:
        self.arguments = arguments
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
    assert runner.arguments == [
        "codex", "exec", "-C", str(worktree.resolve()), "--json", prompt
    ]
    assert "--last" not in runner.arguments


def test_resumes_explicit_session_in_explicit_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = FakeRunner(CommandResult(0, jsonl("thread-123", "Correção concluída")))

    execution = CodexAdapter(runner).resume(worktree, "thread-123", "Aplique a correção")

    assert execution.session_id == "thread-123"
    assert execution.final_message == "Correção concluída"
    assert runner.arguments == [
        "codex", "exec", "-C", str(worktree.resolve()), "resume", "thread-123",
        "Aplique a correção", "--json",
    ]
    assert "--last" not in runner.arguments


def test_resume_accepts_jsonl_without_repeated_thread_event(tmp_path: Path) -> None:
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

    assert CodexAdapter(runner).resume(worktree, "thread-123", "Continue").session_id == "thread-123"


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
        (CommandResult(None, error="Executável não encontrado: codex"), "não encontrado"),
        (CommandResult(None, error="Comando excedeu o timeout de 1800s"), "timeout"),
        (CommandResult(1, stderr="sessão não encontrada"), "código 1.*sessão não encontrada"),
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

    with pytest.raises(CodexError, match="retomar a sessão.*sessão não encontrada"):
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
