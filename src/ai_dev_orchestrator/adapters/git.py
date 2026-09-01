"""Operações locais e seguras de Git para worktrees isolados."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


GIT_TIMEOUT_SECONDS = 20


class GitWorktreeError(Exception):
    """Indica uma falha esperada ao preparar ou remover um Git worktree."""


class ProcessRunner(Protocol):
    """Contrato mínimo do executor de processos usado pelo adapter."""

    def run(self, arguments: Sequence[str]) -> CommandResult:
        """Executa um processo local."""


class GitWorktreeAdapter:
    """Prepara e remove worktrees sem sincronizar ou alterar branches existentes."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = (
            runner if runner is not None else CommandRunner(timeout=GIT_TIMEOUT_SECONDS)
        )

    def validate_repository(self, repository: str | Path) -> Path:
        """Valida o repositório informado e retorna sua raiz descoberta pelo Git."""
        result = self._run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            "validar o repositório",
        )
        root = result.stdout.strip()
        if not root:
            raise GitWorktreeError("Git não retornou a raiz do repositório informado")
        return Path(root)

    def create_worktree(
        self,
        repository: str | Path,
        branch: str,
        worktree_path: str | Path,
        base_ref: str,
    ) -> GitWorktree:
        """Cria uma branch nova e seu worktree, sem sobrescrever estado existente."""
        repository_root = self.validate_repository(repository)
        self._validate_branch(repository_root, branch)
        self._ensure_branch_is_new(repository_root, branch)
        path = self._path_from_repository(repository_root, worktree_path)
        if path.exists():
            raise GitWorktreeError(f"O destino do worktree já existe: {path}")
        self._verify_base_ref(repository_root, base_ref)
        self._run(
            [
                "git", "-C", str(repository_root), "worktree", "add", "-b", branch,
                str(path), base_ref,
            ],
            "criar o worktree",
        )
        return GitWorktree(
            repository_root=repository_root,
            path=path,
            branch=branch,
            base_ref=base_ref,
        )

    def remove_worktree(self, repository: str | Path, worktree_path: str | Path) -> None:
        """Remove um worktree sem forçar a operação ou apagar sua branch."""
        repository_root = self.validate_repository(repository)
        path = self._path_from_repository(repository_root, worktree_path)
        self._run(
            ["git", "-C", str(repository_root), "worktree", "remove", str(path)],
            "remover o worktree",
        )

    def _validate_branch(self, repository_root: Path, branch: str) -> None:
        if not branch:
            raise GitWorktreeError("O nome da branch é inválido")
        result = self.runner.run(
            ["git", "-C", str(repository_root), "check-ref-format", "--branch", branch]
        )
        if result.error:
            raise GitWorktreeError(
                f"Não foi possível validar o nome da branch: {result.error}"
            )
        if not result.succeeded:
            raise GitWorktreeError(f"O nome da branch é inválido: {branch}")

    def _ensure_branch_is_new(self, repository_root: Path, branch: str) -> None:
        result = self.runner.run(
            [
                "git", "-C", str(repository_root), "show-ref", "--verify", "--quiet",
                f"refs/heads/{branch}",
            ]
        )
        if result.error:
            raise GitWorktreeError(
                f"Não foi possível verificar a branch local: {result.error}"
            )
        if result.returncode == 0:
            raise GitWorktreeError(f"A branch local já existe: {branch}")
        if result.returncode != 1:
            self._raise_git_failure(result, "verificar a branch local")

    def _verify_base_ref(self, repository_root: Path, base_ref: str) -> None:
        result = self._run(
            [
                "git", "-C", str(repository_root), "rev-parse", "--verify",
                f"{base_ref}^{{commit}}",
            ],
            "resolver a referência base",
        )
        if not result.stdout.strip():
            raise GitWorktreeError(f"A referência base não pôde ser resolvida: {base_ref}")

    @staticmethod
    def _path_from_repository(repository_root: Path, worktree_path: str | Path) -> Path:
        path = Path(worktree_path)
        return path if path.is_absolute() else repository_root / path

    def _run(self, arguments: list[str], operation: str) -> CommandResult:
        result = self.runner.run(arguments)
        if result.error:
            raise GitWorktreeError(
                f"Não foi possível executar Git ao {operation}: {result.error}"
            )
        if not result.succeeded:
            self._raise_git_failure(result, operation)
        return result

    @staticmethod
    def _raise_git_failure(result: CommandResult, operation: str) -> None:
        detail = result.stderr.strip() or result.stdout.strip()
        message = f"Git retornou código {result.returncode} ao {operation}"
        if detail:
            message = f"{message}: {detail}"
        raise GitWorktreeError(message)
