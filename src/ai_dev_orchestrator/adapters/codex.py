"""Execução headless e com sessão do provider Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.infrastructure.process import (
    CommandResult, CommandRunner, OutputPolicy,
)
from ai_dev_orchestrator.domain.provider import (
    ProviderFailure, ProviderFailureKind, classify_provider_text,
    FAILURE_MESSAGES, FAILURE_PRECEDENCE, reliable_retry_at, textual_retry_at, classify_process_failure,
)


CODEX_TIMEOUT_SECONDS = 30 * 60


class CodexError(Exception):
    """Indica uma falha esperada ao executar ou retomar o Codex CLI."""


class CodexProviderFailure(ProviderFailure, CodexError):
    """Diagnóstico tipado compatível com a fronteira pública do adapter."""


@dataclass(frozen=True)
class CodexExecution:
    """Resultado estruturado de uma execução headless do Codex."""

    session_id: str
    final_message: str
    stdout: str
    stderr: str
    succeeded: bool


class ProcessRunner(Protocol):
    """Contrato mínimo do executor de processos usado pelo adapter."""

    def run(
        self, arguments: Sequence[str], input_text: str | None = None, *,
        stdout_policy: OutputPolicy = OutputPolicy.UTF8_STRICT,
    ) -> CommandResult:
        """Executa um processo local."""


class CodexAdapter:
    """Executa o Codex CLI em um worktree informado, sem montar prompts."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        timeout: float = CODEX_TIMEOUT_SECONDS,
        model: str = "default",
    ) -> None:
        self.runner = runner if runner is not None else CommandRunner(timeout=timeout)
        self.model = model

    def execute(self, worktree: str | Path, prompt: str) -> CodexExecution:
        """Inicia uma sessão persistida do Codex no worktree explicitamente informado."""
        path = self._validate_worktree(worktree)
        arguments = ["codex", "exec", "-C", str(path), "--json"]
        if self.model != "default":
            arguments.extend(["--model", self.model])
        result, session_id, final_message = self._run([*arguments, "-"], prompt, "executar")
        assert session_id is not None
        return CodexExecution(
            session_id=session_id,
            final_message=final_message,
            stdout=result.stdout,
            stderr=result.stderr,
            succeeded=True,
        )

    def resume(
        self, worktree: str | Path, session_id: str, prompt: str
    ) -> CodexExecution:
        """Envia um prompt novo à sessão identificada explicitamente pelo chamador."""
        if not session_id.strip():
            raise CodexError("O identificador da sessão Codex é obrigatório para retomar")
        path = self._validate_worktree(worktree)
        arguments = ["codex", "exec", "-C", str(path), "--json"]
        if self.model != "default":
            arguments.extend(["--model", self.model])
        result, returned_session_id, final_message = self._run(
            [*arguments, "resume", session_id, "-"],
            prompt,
            "retomar a sessão",
            expected_session=session_id,
        )
        assert returned_session_id is not None
        if returned_session_id != session_id:
            raise CodexError(
                "Codex retornou uma sessão diferente da solicitada ao retomar: "
                f"{returned_session_id}"
            )
        return CodexExecution(
            session_id=returned_session_id,
            final_message=final_message,
            stdout=result.stdout,
            stderr=result.stderr,
            succeeded=True,
        )

    @staticmethod
    def _validate_worktree(worktree: str | Path) -> Path:
        path = Path(worktree)
        if not path.is_dir():
            raise CodexError(f"O worktree informado não é um diretório acessível: {path}")
        return path.resolve()

    def _run(self, arguments: list[str], input_text: str, operation: str,
             expected_session: str | None = None) -> tuple[CommandResult, str | None, str]:
        result = self.runner.run(arguments, input_text=input_text,
                                 stdout_policy=OutputPolicy.UTF8_STRICT)
        events = []
        malformed = False
        lines = result.stdout.splitlines()
        if "stdout" in result.encoding_errors:
            # Somente linhas completas anteriores à corrupção podem provar o ID.
            # Esse prefixo nunca é aceito como resultado ou classificação remota.
            lines = []
            for raw in result.stdout_bytes.splitlines(keepends=True):
                if not raw.endswith(b"\n"):
                    break
                try:
                    lines.append(raw.decode("utf-8", errors="strict"))
                except UnicodeDecodeError:
                    break
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError()
                events.append(event)
            except (ValueError, json.JSONDecodeError):
                malformed = True
                break
        sessions = {e["thread_id"] for e in events if e.get("type") == "thread.started"
                    and isinstance(e.get("thread_id"), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", e["thread_id"])}
        session = expected_session or (next(iter(sessions)) if len(sessions) == 1 else None)

        def fail(kind, source, retry_at=None, message=None):
            raise CodexProviderFailure(
                "codex", kind, message or FAILURE_MESSAGES[kind], datetime.now(timezone.utc),
                retry_at, session, result.returncode, source,
            )

        if len(sessions) > 1 or (expected_session and sessions - {expected_session}):
            fail(ProviderFailureKind.PROTOCOL_ERROR, "thread.started",
                 message="Codex retornou uma sessão diferente da solicitada")
        if any(e.get("type") == "thread.started" and (not isinstance(e.get("thread_id"), str) or e["thread_id"] not in sessions) for e in events):
            fail(ProviderFailureKind.PROTOCOL_ERROR, "thread.started")
        if result.error:
            fail(classify_process_failure(result.failure_kind), "processo")
        if malformed:
            fail(ProviderFailureKind.MALFORMED_JSON, "stdout", message="Codex retornou JSONL inválido")
        structured = self._structured_failure(events)
        if structured or not result.succeeded:
            kind, retry = structured or (classify_provider_text(result.stderr), None)
            # stderr pode descrever uma sessão ausente, mas nunca prova sucesso.
            message = None
            if kind == ProviderFailureKind.UNKNOWN:
                message = f"Codex retornou código {result.returncode} ao {operation}; saída omitida"
            fail(kind, "JSONL" if structured else "stderr", retry, message)
        try:
            session_id, final_message = self._parse_events(events, require_session=True)
        except CodexError as error:
            fail(ProviderFailureKind.PROTOCOL_ERROR, "JSONL", message=str(error))
        return result, session_id, final_message

    @staticmethod
    def _structured_failure(events: list[dict[str, Any]]) -> tuple[ProviderFailureKind, datetime | None] | None:
        mapping = {
            "rate_limit": ProviderFailureKind.TRANSIENT_RATE_LIMIT,
            "quota_exceeded": ProviderFailureKind.TERMINAL_QUOTA,
            "authentication": ProviderFailureKind.AUTH_ERROR,
            "network": ProviderFailureKind.NETWORK_ERROR,
            "model_unavailable": ProviderFailureKind.MODEL_UNAVAILABLE,
            "timeout": ProviderFailureKind.TIMEOUT,
        }
        signals: list[tuple[ProviderFailureKind, datetime | None]] = []
        for event in events:
            error = event.get("error")
            if event.get("type") not in {"error", "turn.failed"} and not isinstance(error, dict):
                continue
            detail = error if isinstance(error, dict) else event
            message = detail.get("message", "")
            message = message if isinstance(message, str) else ""
            kinds = {mapping.get(str(detail.get("code", "")).casefold(), ProviderFailureKind.UNKNOWN),
                     classify_provider_text(message)}
            kind = next(k for k in FAILURE_PRECEDENCE if k in kinds)
            retry = reliable_retry_at(detail.get("retry_at")) or textual_retry_at(message)
            signals.append((kind, retry))
        if not signals:
            return None
        kind = next(k for k in FAILURE_PRECEDENCE if any(s[0] == k for s in signals))
        retries = {r for k, r in signals if k == kind and r is not None}
        # Sinais conflitantes não autorizam escolher uma janela arbitrária.
        return kind, next(iter(retries)) if len(retries) == 1 else None

    @classmethod
    def _parse_events(cls, events: list[dict[str, Any]], require_session: bool) -> tuple[str | None, str]:
        session_id: str | None = None
        final_message: str | None = None
        has_event = False
        completed = False
        for event in events:
            has_event = True
            if event.get("type") == "thread.started":
                session_id = cls._required_string(event, "thread_id", "thread.started")
            if event.get("type") == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise CodexError("Codex retornou item.completed inválido")
                if item.get("type") == "agent_message":
                    final_message = cls._required_string(item, "text", "agent_message")
            if event.get("type") == "turn.completed":
                completed = True
        if not has_event:
            raise CodexError("Codex não retornou eventos JSONL")
        if require_session and session_id is None:
            raise CodexError("Codex concluiu a execução sem retornar o identificador da sessão")
        if final_message is None:
            raise CodexError("Codex concluiu a execução sem retornar a mensagem final")
        if not completed:
            raise CodexError("Codex não retornou o evento de conclusão da execução")
        return session_id, final_message

    @staticmethod
    def _required_string(event: dict[str, Any], field: str, event_name: str) -> str:
        value = event.get(field)
        if not isinstance(value, str) or not value:
            raise CodexError(
                f"Codex retornou {event_name} inválido: campo '{field}' deve ser texto"
            )
        return value
