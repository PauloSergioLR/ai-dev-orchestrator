"""Descoberta somente leitura e gravação atômica do perfil por projeto."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.infrastructure.process import CommandRunner


class ProjectInitError(Exception):
    """A configuração não pode ser descoberta ou gravada com segurança."""


@dataclass(frozen=True)
class ProjectDiscovery:
    repository_path: Path
    remote_name: str
    remote_url: str | None
    owner: str | None
    repository: str | None
    default_branch: str | None
    branches: tuple[str, ...]
    suggested_base_branch: str | None
    evidence: tuple[str, ...]
    github_projects: tuple[int, ...] = ()
    remote_names: tuple[str, ...] = ()
    gemini_models: tuple[str, ...] = ()


class ProjectInitService:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner(timeout=15)

    def discover(self, cwd: Path) -> ProjectDiscovery:
        root_result = self.runner.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        if not root_result.succeeded:
            raise ProjectInitError(
                "O diretório atual não pertence a um repositório Git"
            )
        root = Path(root_result.stdout.strip()).resolve()
        remotes = self.runner.run(["git", "remote"], cwd=root)
        remote_names = (
            tuple(name.strip() for name in remotes.stdout.splitlines() if name.strip())
            if remotes.succeeded
            else ()
        )
        remote_name = (
            "origin"
            if "origin" in remote_names
            else remote_names[0]
            if len(remote_names) == 1
            else "origin"
        )
        remote = self.runner.run(["git", "remote", "get-url", remote_name], cwd=root)
        remote_url = remote.stdout.strip() if remote.succeeded else None
        owner, repository = _parse_github_remote(remote_url)
        refs = self.runner.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
                "refs/remotes",
            ],
            cwd=root,
        )
        branches = (
            _normalize_branches(refs.stdout, remote_name) if refs.succeeded else ()
        )
        default_branch = None
        github_projects: tuple[int, ...] = ()
        if owner and repository:
            gh = self.runner.run(
                [
                    "gh",
                    "repo",
                    "view",
                    f"{owner}/{repository}",
                    "--json",
                    "defaultBranchRef",
                ],
                cwd=root,
            )
            if gh.succeeded:
                try:
                    payload = json.loads(gh.stdout)
                    value = payload.get("defaultBranchRef", {}).get("name")
                    default_branch = value if isinstance(value, str) and value else None
                except (json.JSONDecodeError, AttributeError):
                    pass
            if default_branch is None:
                symbolic = self.runner.run(
                    [
                        "git",
                        "symbolic-ref",
                        "--short",
                        f"refs/remotes/{remote_name}/HEAD",
                    ],
                    cwd=root,
                )
                if symbolic.succeeded:
                    prefix = f"{remote_name}/"
                    value = symbolic.stdout.strip()
                    default_branch = (
                        value[len(prefix) :] if value.startswith(prefix) else None
                    )
            projects = self.runner.run(
                ["gh", "project", "list", "--owner", owner, "--format", "json"],
                cwd=root,
            )
            if projects.succeeded:
                try:
                    payload = json.loads(projects.stdout)
                    entries = payload.get("projects", [])
                    github_projects = tuple(
                        value["number"]
                        for value in entries
                        if isinstance(value, dict)
                        and isinstance(value.get("number"), int)
                        and value["number"] > 0
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass
        evidence: list[str] = []
        suggested = None
        for name in ("AGENTS.md", "CONTRIBUTING.md", "README.md"):
            path = root / name
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")[:200_000]
            except (OSError, UnicodeError):
                continue
            if re.search(
                r"(?i)\b(base|branch|fluxo|flow|pull request|pr)\b.{0,80}\bdevelop\b",
                content,
            ):
                evidence.append(f"{name} indica fluxo baseado em develop")
                suggested = "develop"
        if suggested not in branches:
            suggested = default_branch if default_branch in branches else None
        model_result = self.runner.run(["agy", "models"], cwd=root)
        gemini_models = (
            _parse_model_listing(model_result.stdout) if model_result.succeeded else ()
        )
        return ProjectDiscovery(
            root,
            remote_name,
            remote_url,
            owner,
            repository,
            default_branch,
            branches,
            suggested,
            tuple(evidence),
            github_projects,
            remote_names,
            gemini_models,
        )

    def write(self, path: Path, config: OrchestratorConfig) -> None:
        """Valida antes e substitui atomicamente; nunca deixa TOML parcial."""
        content = render_toml(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ProjectInitError(
                f"Não foi possível gravar {path}: {error}"
            ) from error


def _parse_github_remote(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url.strip())
    return (match.group(1), match.group(2)) if match else (None, None)


def _normalize_branches(output: str, remote: str) -> tuple[str, ...]:
    values: set[str] = set()
    for raw in output.splitlines():
        branch = raw.strip()
        if not branch or branch.endswith("/HEAD"):
            continue
        if branch.startswith(f"{remote}/"):
            branch = branch[len(remote) + 1 :]
        values.add(branch)
    preferred = {"develop": 0, "main": 1, "master": 2}
    return tuple(sorted(values, key=lambda value: (preferred.get(value, 3), value)))


def _parse_model_listing(output: str) -> tuple[str, ...]:
    """Aceita somente identificadores inequívocos emitidos um por linha."""
    models: list[str] = []
    for line in output.splitlines():
        candidate = line.strip().removeprefix("-").strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,}", candidate):
            models.append(candidate)
    return tuple(dict.fromkeys(models))


def render_toml(config: OrchestratorConfig) -> str:
    def q(value: object) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    def array(values: tuple[str, ...]) -> str:
        return "[" + ", ".join(q(value) for value in values) + "]"

    return f"""[github]
owner = {q(config.github.owner)}
repository = {q(config.github.repository)}
project_number = {config.github.project_number}
ready_status = {q(config.github.ready_status)}
in_progress_status = {q(config.github.in_progress_status)}
ai_review_status = {q(config.github.ai_review_status)}
done_status = {q(config.github.done_status)}
pull_request_target = {q(config.github.pull_request_target)}
protected_branches = {array(config.github.protected_branches)}
status_field_name = {q(config.github.status_field_name)}

[workspace]
repository_path = {q(config.workspace.repository_path.as_posix())}
worktrees_dir = {q(config.workspace.worktrees_dir.as_posix())}
base_branch = {q(config.workspace.base_branch)}
remote_name = {q(config.workspace.remote_name)}

[providers]
codex_model = {q(config.providers.codex_model)}
gemini_model = {q(config.providers.gemini_model)}

[execution]
max_attempts = {config.execution.max_attempts}
max_parallel_runs = {config.execution.max_parallel_runs}
auto_merge = {str(config.execution.auto_merge).lower()}
merge_timeout_seconds = {config.execution.merge_timeout_seconds}

[state]
database_path = {q(config.state.database_path.as_posix())}

[ci]
required_checks = {array(config.ci.required_checks)}
poll_interval_seconds = {config.ci.poll_interval_seconds}
timeout_seconds = {config.ci.timeout_seconds}

[convergence]
poll_interval_seconds = {config.convergence.poll_interval_seconds}
timeout_seconds = {config.convergence.timeout_seconds}

[review]
provider = {q(config.review.provider)}
timeout_seconds = {config.review.timeout_seconds}
max_correction_attempts = {config.review.max_correction_attempts}
blocking_severities = {array(config.review.blocking_severities)}

[supervisor]
poll_interval_seconds = {config.supervisor.poll_interval_seconds}
max_sleep_seconds = {config.supervisor.max_sleep_seconds}
""" + (
        f"retry_without_reset_seconds = {config.supervisor.retry_without_reset_seconds}\n"
        if config.supervisor.retry_without_reset_seconds is not None
        else ""
    )
