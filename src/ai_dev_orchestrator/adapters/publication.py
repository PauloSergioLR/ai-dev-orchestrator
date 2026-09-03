"""Publicação segura de alterações já validadas no Git."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


GIT_PUBLICATION_TIMEOUT_SECONDS = 30


class GitPublicationError(Exception):
    """Indica que uma etapa não destrutiva de publicação Git falhou."""


class ProcessRunner(Protocol):
    def run(self, arguments: Sequence[str], cwd: str | Path | None = None) -> CommandResult: ...


class GitPublicationAdapter:
    """Stageia, commita e envia uma branch sem reescrever histórico."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or CommandRunner(timeout=GIT_PUBLICATION_TIMEOUT_SECONDS)

    def commit(self, worktree: str | Path, issue_number: int) -> str:
        return self._commit(worktree, f"feat: implementa issue #{issue_number}")

    def commit_correction(self, worktree: str | Path) -> str:
        """Cria o commit determinístico de uma correção apontada pelo reviewer."""
        return self._commit(worktree, "fix: corrige findings do reviewer")

    def current_head(self, worktree: str | Path) -> str:
        """Obtém o HEAD local sem modificar o worktree."""
        sha = self._run(["git", "rev-parse", "HEAD"], worktree, "obter HEAD local").stdout.strip()
        if not sha:
            raise GitPublicationError("Git não retornou o HEAD local")
        return sha

    def merge_state(self, worktree: str | Path) -> tuple[str, str]:
        """Confirma branch e ausência de alterações locais antes do merge remoto."""
        branch = self._run(["git", "branch", "--show-current"], worktree, "obter branch local").stdout.strip()
        if not branch:
            raise GitPublicationError("Worktree está em HEAD destacado")
        dirty = self._run(["git", "status", "--porcelain", "--untracked-files=all"], worktree, "verificar estado local").stdout
        if dirty.strip():
            raise GitPublicationError("Worktree possui alterações não commitadas")
        return branch, self.current_head(worktree)

    def remote_head(self, worktree: str | Path, remote_name: str, branch: str) -> str | None:
        """Lê o HEAD remoto da branch sem efetuar push."""
        result = self._run(["git", "ls-remote", "--heads", remote_name, f"refs/heads/{branch}"], worktree, "ler branch remota")
        line = result.stdout.strip()
        if not line:
            return None
        sha = line.split()[0]
        if not sha:
            raise GitPublicationError("Git não retornou o HEAD remoto")
        return sha

    def _commit(self, worktree: str | Path, message: str) -> str:
        status = self._run(["git", "status", "--porcelain", "--untracked-files=all"], worktree, "verificar alterações")
        if not status.stdout.strip():
            raise GitPublicationError("Não há alterações versionáveis para commitar")
        self._run(["git", "add", "-A"], worktree, "stagear alterações")
        self._run(["git", "diff", "--cached", "--check"], worktree, "validar diff staged")
        self._run(["git", "commit", "-m", message], worktree, "criar commit")
        sha = self._run(["git", "rev-parse", "HEAD"], worktree, "obter SHA do commit").stdout.strip()
        if not sha:
            raise GitPublicationError("Git não retornou o SHA do commit criado")
        return sha

    def push(self, worktree: str |Path, remote_name: str, branch: str) -> None:
        self._run(["git", "push", "-u", remote_name, branch], worktree, "enviar a branch")

    def _run(self, arguments: list[str], worktree: str | Path, operation: str) -> CommandResult:
        result = self.runner.run(arguments, cwd=worktree)
        if result.error:
            raise GitPublicationError(f"Não foi possível executar Git ao {operation}: {result.error}")
        if not result.succeeded:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"Git retornou código {result.returncode} ao {operation}"
            raise GitPublicationError(f"{message}: {detail}" if detail else message)
        return result
