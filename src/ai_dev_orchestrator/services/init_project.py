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
from ai_dev_orchestrator.domain.project_contract import ProjectContract
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver


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
    contract: ProjectContract | None = None


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
                        and not isinstance(value.get("number"), bool)
                        and value["number"] > 0
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass
        evidence: list[str] = []
        suggested = None
        for name in ("AGENTS.md", "CONTRIBUTING.md", "README.md"):
            path = root / name
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            try:
                with path.open(encoding="utf-8") as stream:
                    content = stream.read(200_000)
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
        contract = None
        selected_base = suggested or default_branch
        if selected_base:
            contract = ProjectCapabilityResolver().resolve(
                root,
                repository_identity=(f"{owner}/{repository}" if owner and repository else root.name),
                base_branch=selected_base,
                pull_request_target=selected_base,
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
            contract,
        )

    def discover_status_options(self, owner: str, project_number: int, cwd: Path) -> tuple[str, ...]:
        """Lê opções do campo Status sem depender da view board/table/list."""
        result = self.runner.run(
            ["gh", "project", "field-list", str(project_number), "--owner", owner, "--format", "json"],
            cwd=cwd,
        )
        if not result.succeeded:
            return ()
        try:
            fields = json.loads(result.stdout).get("fields", [])
        except (json.JSONDecodeError, AttributeError):
            return ()
        matches = [field for field in fields if isinstance(field, dict) and field.get("name") == "Status"]
        if len(matches) != 1 or not isinstance(matches[0].get("options"), list):
            return ()
        return tuple(
            option["name"] for option in matches[0]["options"]
            if isinstance(option, dict) and isinstance(option.get("name"), str) and option["name"]
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
    match = re.fullmatch(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", url.strip())
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
    """Serializa todos os campos tipados, preservando defaults e overrides customizados."""
    def value(item: object) -> str:
        if isinstance(item, Path):
            return json.dumps(item.as_posix(), ensure_ascii=False)
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, bool):
            return str(item).lower()
        if isinstance(item, (int, float)):
            return str(item)
        if isinstance(item, (tuple, list)):
            return "[" + ", ".join(value(child) for child in item) + "]"
        if isinstance(item, dict):
            return "{ " + ", ".join(
                f"{value(key)} = {value(child)}" for key, child in item.items() if child is not None
            ) + " }"
        raise ProjectInitError("Tipo não suportado ao serializar configuração")

    data = config.model_dump(mode="python")
    # Ausência de required_checks conserva a descoberta automática do contrato.
    if "required_checks" not in config.ci.model_fields_set:
        data["ci"].pop("required_checks", None)
    sections = []
    for section, entries in data.items():
        lines = [f"[{section}]"]
        lines.extend(f"{key} = {value(item)}" for key, item in entries.items() if item is not None)
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"
