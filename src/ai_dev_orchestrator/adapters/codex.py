"""Execução headless e com sessão do provider Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.domain.provider import (
    ProviderFailure, ProviderFailureKind, classify_provider_text,
)


CODEX_TIMEOUT_SECONDS = 30 * 60


class CodexError(Exception):
    """Indica uma falha esperada ao executar ou retomar o Codex CLI."""


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
        self, arguments: Sequence[str], input_text: str | None = None
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
        result = self._run([*arguments, "-"], prompt, "executar")
        session_id, final_message = self._parse_jsonl(result.stdout, require_session=True)
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
        result = self._run(
            [*arguments, "resume", session_id, "-"],
            prompt,
            "retomar a sessão",
        )
        returned_session_id, final_message = self._parse_jsonl(
            result.stdout, require_session=True
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

    def _run(self, arguments: list[str], input_text: str, operation: str) -> CommandResult:
        result = self.runner.run(arguments, input_text=input_text)
        if result.error:
            raise CodexError(f"Não foi possível executar Codex ao {operation}: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            structured = self._structured_failure(result.stdout)
            kind = structured[0] if structured else classify_provider_text(detail)
            retry_at = structured[1] if structured else None
            if kind is not ProviderFailureKind.UNKNOWN:
                raise ProviderFailure(
                    "codex", kind, "Falha reportada pela CLI", datetime.now(timezone.utc), retry_at,
                    self._partial_session(result.stdout),
                )
            message = f"Codex retornou código {result.returncode} ao {operation}"
            if detail:
                message = f"{message}: {detail}"
            raise CodexError(message)
        return result

    @staticmethod
    def _partial_session(stdout: str) -> str | None:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                value = event.get("thread_id")
                return value if isinstance(value, str) and value else None
        return None

    @staticmethod
    def _structured_failure(stdout: str) -> tuple[ProviderFailureKind, datetime | None] | None:
        mapping = {
            "rate_limit": ProviderFailureKind.TRANSIENT_RATE_LIMIT,
            "quota_exceeded": ProviderFailureKind.TERMINAL_QUOTA,
            "authentication": ProviderFailureKind.AUTH_ERROR,
            "network": ProviderFailureKind.NETWORK_ERROR,
            "model_unavailable": ProviderFailureKind.MODEL_UNAVAILABLE,
        }
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            error = event.get("error")
            if not isinstance(error, dict):
                continue
            code = str(error.get("code", "")).casefold()
            if code not in mapping:
                continue
            retry_at = None
            raw_retry = error.get("retry_at")
            if isinstance(raw_retry, str):
                try:
                    retry_at = datetime.fromisoformat(raw_retry.replace("Z", "+00:00"))
                    if retry_at.tzinfo is None:
                        retry_at = None
                except ValueError:
                    retry_at = None
            return mapping[code], retry_at
        return None

    @classmethod
    def _parse_jsonl(cls, stdout: str, require_session: bool) -> tuple[str | None, str]:
        session_id: str | None = None
        final_message: str | None = None
        has_event = False
        completed = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise CodexError("Codex retornou JSONL inválido") from error
            if not isinstance(event, dict):
                raise CodexError("Codex retornou um evento JSONL inválido: objeto esperado")
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
