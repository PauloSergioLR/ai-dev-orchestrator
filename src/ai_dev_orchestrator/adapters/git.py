"""Operações locais e seguras de Git para worktrees isolados."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Protocol, Sequence

from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.domain.base_ref import PreparedBase
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner
from ai_dev_orchestrator.infrastructure.redaction import RedactedError


GIT_TIMEOUT_SECONDS = 20


class GitWorktreeError(RedactedError):
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

    def verify_remote_identity(self, repository: str | Path, remote_name: str, expected_repository: str) -> None:
        """Fetch e push devem apontar exclusivamente ao mesmo repositório GitHub."""
        for extra in ([], ["--push"]):
            output = self._run(
                ["git", "-C", str(repository), "remote", "get-url", *extra, "--all", remote_name],
                "verificar identidade do remote",
            ).stdout.splitlines()
            if len(output) != 1:
                raise GitWorktreeError("Remote ausente ou com múltiplos destinos; identidade ambígua")
            match = re.fullmatch(
                r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
                output[0].strip(),
            )
            if match is None or match.group(1).casefold() != expected_repository.casefold():
                raise GitWorktreeError("Identidade do remote diverge do repositório configurado")

    def prepare_remote_base(
        self,
        repository: str | Path,
        remote_name: str,
        base_ref: str,
        branch: str,
    ) -> PreparedBase:
        """Atualiza a base remota e recusa colisões antes de criar um worktree."""
        repository_root = self.validate_repository(repository)
        self._validate_branch(repository_root, branch)
        self._ensure_branch_is_new(repository_root, branch)
        base_branch = self._remote_branch(remote_name, base_ref)
        remote_ref = f"refs/remotes/{remote_name}/{base_branch}"
        self._run(
            [
                "git", "-C", str(repository_root), "fetch", "--no-tags", "--", remote_name,
                f"refs/heads/{base_branch}:{remote_ref}",
            ],
            "sincronizar a base remota",
        )
        sha = self._resolve_commit(repository_root, remote_ref)
        self._ensure_remote_branch_is_new(repository_root, remote_name, branch)
        return PreparedBase(remote_ref, sha)

    def create_detached_worktree(
        self,
        repository: str | Path,
        worktree_path: str | Path,
        commit_sha: str,
    ) -> Path:
        """Materializa snapshot detached temporário de um commit já identificado."""
        repository_root = self.validate_repository(repository)
        path = self._path_from_repository(repository_root, worktree_path)
        if path.exists():
            raise GitWorktreeError(f"O destino do snapshot já existe: {path}")
        resolved = self._resolve_commit(repository_root, commit_sha)
        if resolved.casefold() != commit_sha.casefold():
            raise GitWorktreeError("O SHA solicitado não identifica exatamente o commit resolvido")
        self._run(
            [
                "git", "-C", str(repository_root), "worktree", "add", "--detach",
                str(path), commit_sha,
            ],
            "materializar snapshot detached",
        )
        return path

    def remove_worktree(self, repository: str | Path, worktree_path: str | Path) -> None:
        """Remove um worktree sem forçar a operação ou apagar sua branch."""
        repository_root = self.validate_repository(repository)
        path = self._path_from_repository(repository_root, worktree_path)
        self._run(
            ["git", "-C", str(repository_root), "worktree", "remove", str(path)],
            "remover o worktree",
        )

    def worktree_is_clean(self, repository: str | Path, worktree_path: str | Path) -> bool:
        """Só autoriza remoção quando Git confirma ausência de alterações locais."""
        repository_root = self.validate_repository(repository)
        path = self._path_from_repository(repository_root, worktree_path)
        result = self._run(
            ["git", "-C", str(path), "status", "--porcelain", "--ignored", "--untracked-files=all"],
            "verificar alterações no worktree",
        )
        return not result.stdout.strip()

    def worktree_is_registered(
        self, repository: str | Path, worktree_path: str | Path
    ) -> bool:
        """Distingue worktree Git registrado de diretório órfão no mesmo caminho."""
        repository_root = self.validate_repository(repository)
        expected = self._path_from_repository(repository_root, worktree_path).resolve()
        result = self._run(
            ["git", "-C", str(repository_root), "worktree", "list", "--porcelain"],
            "listar worktrees registrados",
        )
        registered = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        }
        return expected in registered

    @staticmethod
    def validate_cleanup_path(worktree_path: str | Path, allowed_root: str | Path) -> Path:
        """Exige filho direto e recusa links/junctions em toda a cadeia de diretórios."""
        raw = Path(worktree_path)
        root = Path(allowed_root)
        if not raw.is_absolute() or not root.is_absolute():
            raise GitWorktreeError("Cleanup exige caminhos absolutos")
        for candidate in (raw, root, *raw.parents, *root.parents):
            if candidate.is_symlink() or candidate.is_junction():
                raise GitWorktreeError("Cleanup recusa link simbólico ou junction no caminho")
        path = raw.resolve()
        if path.parent != root.resolve() or path.name == ".orchestrator-quarantine":
            raise GitWorktreeError("Caminho não é um alvo de quarentena ou cleanup sob worktrees_dir")
        return path

    def verify_cleanup_worktree(
        self, repository: str | Path, worktree_path: str | Path,
        branch: str, expected_sha: str,
    ) -> None:
        """Confirma que o worktree ainda pertence ao repositório, branch e commit registrados."""
        root = self.validate_repository(repository)
        if root.resolve() != Path(repository).resolve():
            raise GitWorktreeError("Raiz do repositório diverge da configuração")
        expected_common = self._run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            "identificar o repositório",
        ).stdout.strip()
        actual_common = self._run(
            ["git", "-C", str(worktree_path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            "identificar o repositório do worktree",
        ).stdout.strip()
        actual_branch = self._run(
            ["git", "-C", str(worktree_path), "symbolic-ref", "--quiet", "HEAD"],
            "identificar a branch do worktree",
        ).stdout.strip()
        if (not expected_common or not actual_common
                or Path(expected_common).resolve() != Path(actual_common).resolve()
                or actual_branch != f"refs/heads/{branch}"
                or self._resolve_commit(Path(worktree_path), "HEAD") != expected_sha):
            raise GitWorktreeError("Identidade atual do worktree diverge da execução; preservado")

    def local_branch_head(self, repository: str | Path, branch: str) -> str | None:
        if not self.local_branch_exists(repository, branch):
            return None
        return self._resolve_commit(Path(repository), f"refs/heads/{branch}")

    @staticmethod
    def remove_empty_orphan_directory(
        worktree_path: str | Path, allowed_root: str | Path
    ) -> None:
        """Remove só diretório vazio sob worktrees_dir; conteúdo desconhecido é intocável."""
        path = GitWorktreeAdapter.validate_cleanup_path(worktree_path, allowed_root)
        if not path.is_dir():
            raise GitWorktreeError("Diretório órfão não é um alvo removível")
        try:
            path.rmdir()
        except OSError as error:
            raise GitWorktreeError(
                "Diretório órfão contém arquivos ou não pôde ser removido; preservado"
            ) from error

    @staticmethod
    def quarantine_orphan_directory(
        worktree_path: str | Path,
        allowed_root: str | Path,
        execution_id: str,
    ) -> Path:
        """Move órfão não vazio para quarentena recuperável, sem apagar conteúdo."""
        root = Path(allowed_root).resolve()
        path = GitWorktreeAdapter.validate_cleanup_path(worktree_path, allowed_root)
        if path.parent != root or not path.is_dir() or path.name == ".orchestrator-quarantine":
            raise GitWorktreeError("Diretório órfão não é um alvo de quarentena")
        quarantine = root / ".orchestrator-quarantine"
        if quarantine.is_symlink() or quarantine.is_junction():
            raise GitWorktreeError("Quarentena é um link simbólico; conteúdo preservado")
        quarantine.mkdir(exist_ok=True)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", execution_id):
            raise GitWorktreeError("Identificador da execução inválido para quarentena")
        target = quarantine / f"{execution_id}--{path.name}"
        if target.exists() or target.is_symlink():
            raise GitWorktreeError(
                f"Destino de quarentena já existe; nenhum conteúdo foi movido: {target}"
            )
        path.rename(target)
        return target

    def delete_local_branch(self, repository: str | Path, branch: str, expected_sha: str) -> None:
        """Remove ref integrada somente se ainda apontar para o commit comprovado."""
        repository_root = self.validate_repository(repository)
        self._validate_branch(repository_root, branch)
        self._validate_sha(expected_sha)
        registered = self._run(
            ["git", "-C", str(repository_root), "worktree", "list", "--porcelain"],
            "confirmar branch sem worktree",
        ).stdout.splitlines()
        if f"branch refs/heads/{branch}" in registered:
            raise GitWorktreeError("Branch ainda está em uso por worktree; preservada")
        self._run(
            ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", expected_sha, "HEAD"],
            "confirmar integração da branch local",
        )
        self._run(
            ["git", "-C", str(repository_root), "update-ref", "-d", f"refs/heads/{branch}", expected_sha],
            "remover a branch local",
        )

    def local_branch_exists(self, repository: str | Path, branch: str) -> bool:
        repository_root = self.validate_repository(repository)
        result = self.runner.run(
            ["git", "-C", str(repository_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        )
        if result.error:
            raise GitWorktreeError(f"Não foi possível verificar a branch local: {result.error}")
        if result.returncode not in {0, 1}:
            self._raise_git_failure(result, "verificar a branch local")
        return result.returncode == 0

    def delete_remote_branch(self, repository: str | Path, remote_name: str, branch: str, expected_sha: str) -> None:
        """Exclui somente a ref remota cujo SHA ainda é exatamente o mergeado."""
        repository_root = self.validate_repository(repository)
        self._validate_branch(repository_root, branch)
        self._validate_sha(expected_sha)
        self._run(
            ["git", "-C", str(repository_root), "push", f"--force-with-lease=refs/heads/{branch}:{expected_sha}", "--", remote_name, f":refs/heads/{branch}"],
            "remover a branch remota",
        )

    @staticmethod
    def _validate_sha(sha: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
            raise GitWorktreeError("SHA esperado para exclusão é inválido")

    def remote_branch_exists(self, repository: str | Path, remote_name: str, branch: str) -> bool:
        repository_root = self.validate_repository(repository)
        result = self.runner.run(
            ["git", "-C", str(repository_root), "ls-remote", "--exit-code", "--heads", "--", remote_name, f"refs/heads/{branch}"]
        )
        if result.error:
            raise GitWorktreeError(f"Não foi possível verificar a branch remota: {result.error}")
        if result.returncode not in {0, 2}:
            self._raise_git_failure(result, "verificar a branch remota")
        return result.returncode == 0

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

    def _ensure_remote_branch_is_new(
        self, repository_root: Path, remote_name: str, branch: str
    ) -> None:
        result = self.runner.run(
            [
                "git", "-C", str(repository_root), "ls-remote", "--exit-code", "--heads",
                "--", remote_name, f"refs/heads/{branch}",
            ]
        )
        if result.error:
            raise GitWorktreeError(
                f"Não foi possível verificar a branch remota: {result.error}"
            )
        if result.returncode == 0:
            raise GitWorktreeError(f"A branch remota já existe: {remote_name}/{branch}")
        if result.returncode != 2:
            self._raise_git_failure(result, "verificar a branch remota")

    @staticmethod
    def _remote_branch(remote_name: str, base_ref: str) -> str:
        prefixes = (f"refs/remotes/{remote_name}/", f"{remote_name}/", "refs/heads/")
        branch = base_ref
        for prefix in prefixes:
            if branch.startswith(prefix):
                branch = branch[len(prefix):]
                break
        if not branch or branch.startswith("refs/"):
            raise GitWorktreeError(
                f"A referência base não identifica uma branch remota: {base_ref}"
            )
        return branch

    def _verify_base_ref(self, repository_root: Path, base_ref: str) -> None:
        self._resolve_commit(repository_root, base_ref)

    def _resolve_commit(self, repository_root: Path, base_ref: str) -> str:
        """Resolve uma ref móvel uma vez e devolve a identidade imutável."""
        result = self._run(
            [
                "git", "-C", str(repository_root), "rev-parse", "--verify",
                f"{base_ref}^{{commit}}",
            ],
            "resolver a referência base",
        )
        if not result.stdout.strip():
            raise GitWorktreeError(f"A referência base não pôde ser resolvida: {base_ref}")
        return result.stdout.strip()

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
