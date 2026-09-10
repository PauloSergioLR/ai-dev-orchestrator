"""Descoberta conservadora de capacidades, sem catálogo de stacks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Iterable, Protocol

from ai_dev_orchestrator.domain.project_contract import (
    CommandPlan,
    ContractConfidence,
    ProjectComponent,
    ProjectContract,
    RiskClass,
    SourceEvidence,
)
from ai_dev_orchestrator.infrastructure.process import CommandRunner


class ContractResolutionError(Exception):
    """Não há evidência suficiente para executar comandos automaticamente."""


_SHELL_OPERATORS = re.compile(r"[;&|<>`\r\n]|\$\(")
_REMOTE_WORDS = re.compile(
    r"(?i)(?:^|[-_ :/])(deploy|publish|release|migrations?|migrate|terraform|secret|infrastructure|cloud|remote|staging|production)(?:$|[-_ :/])"
)
_BOOTSTRAP_WORDS = re.compile(
    r"(?i)\b(install|instalar|restore|restaurar|bootstrap|setup|sync|prepare|preparar|dependenc(?:y|ies)|depend[eê]ncias?)\b"
)
_VALIDATION_WORDS = re.compile(
    r"(?i)\b(test(?:s|es)?|check|lint|format|verify|valida(?:te|r|cao|\u00e7\u00e3o)|build|quality|e2e|unit|integration)\b"
)
_IGNORED_DIRS = {".git", ".venv", "node_modules", "vendor", "dist", "build", ".tox"}


@dataclass(frozen=True)
class _Candidate:
    name: str
    command: str
    cwd: str
    evidence: SourceEvidence


class ContractInterpreter(Protocol):
    """Porta opcional para IA; a implementação deve devolver o schema estruturado."""

    def interpret(self, evidence: tuple[SourceEvidence, ...]) -> tuple[CommandPlan, ...]: ...


CONTRACT_COMMAND_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": [
            "capability", "display_name", "argv", "cwd", "timeout_seconds",
            "source_evidence", "confidence", "risk_class",
        ],
        "properties": {
            "capability": {"type": "string"},
            "display_name": {"type": "string"},
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
            "source_evidence": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["path", "kind", "detail", "line"],
                    "properties": {
                        "path": {"type": "string"},
                        "kind": {"type": "string"},
                        "detail": {"type": "string"},
                        "line": {"type": ["integer", "null"]},
                    },
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "risk_class": {"enum": [value.value for value in RiskClass]},
        },
        "additionalProperties": False,
    },
}


class AiProjectContractInterpreter:
    """Adapta um provider de structured output sem lhe conceder execução."""

    def __init__(self, invoker, cwd: Path) -> None:
        self.invoker = invoker
        self.cwd = cwd

    def interpret(self, evidence: tuple[SourceEvidence, ...]) -> tuple[CommandPlan, ...]:
        prompt = (
            "Interprete somente as evidências versionadas abaixo e proponha capacidades "
            "locais de build/teste em argv estruturado. Não invente comandos, não inclua "
            "deploy/operações remotas e cite path/linha em source_evidence.\n\n"
            + json.dumps([item.__dict__ for item in evidence], ensure_ascii=False)
        )
        raw = self.invoker.invoke(prompt, self.cwd, CONTRACT_COMMAND_SCHEMA)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as error:
            raise ContractResolutionError("IA retornou contrato JSON inválido") from error
        if not isinstance(payload, list):
            raise ContractResolutionError("IA retornou contrato fora do schema")
        plans: list[CommandPlan] = []
        for index, item in enumerate(payload, 1):
            if not isinstance(item, dict):
                raise ContractResolutionError("IA retornou gate fora do schema")
            try:
                sources = tuple(SourceEvidence(**source) for source in item["source_evidence"])
                display = item["display_name"]
                plans.append(CommandPlan(
                    name=ProjectCapabilityResolver._slug(display),
                    capability=item["capability"], display_name=display,
                    argv=tuple(item["argv"]), cwd=item["cwd"],
                    timeout_seconds=float(item["timeout_seconds"]),
                    source_evidence=sources, confidence=float(item["confidence"]),
                    risk_class=RiskClass(item["risk_class"]),
                ))
            except (KeyError, TypeError, ValueError) as error:
                raise ContractResolutionError(f"Gate #{index} da IA é inválido") from error
        return tuple(plans)


class ProjectCapabilityResolver:
    """Combina CI, scripts e documentação; somente comandos provados são aceitos."""

    def __init__(
        self,
        interpreter: ContractInterpreter | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.interpreter = interpreter
        self.runner = runner or CommandRunner(timeout=15)

    def resolve(
        self,
        root: Path,
        *,
        repository_identity: str,
        base_branch: str,
        pull_request_target: str,
        protected_branches: tuple[str, ...] = (),
        overrides: tuple[CommandPlan, ...] = (),
    ) -> ProjectContract:
        root = root.resolve()
        if not root.is_dir():
            raise ContractResolutionError(f"Raiz do repositório não existe: {root}")
        ci, ci_names = self._workflow_candidates(root)
        scripts = self._script_candidates(root)
        docs = self._documentation_candidates(root)
        candidates = list(overrides) if overrides else self._select(ci, scripts, docs)
        bootstrap: list[CommandPlan] = []
        gates: list[CommandPlan] = []
        excluded: list[CommandPlan] = []
        rejected: list[str] = []
        for item in candidates:
            try:
                plan = item if isinstance(item, CommandPlan) else self._to_plan(item)
            except ContractResolutionError as error:
                rejected.append(str(error))
                continue
            if plan.risk_class is not RiskClass.SAFE_LOCAL:
                excluded.append(plan)
            elif plan.capability == "bootstrap":
                bootstrap.append(plan)
            else:
                gates.append(plan)
        gates = self._deduplicate(gates)
        bootstrap = self._deduplicate(bootstrap)
        if not gates and self.interpreter is not None:
            interpreted = self.interpreter.interpret(tuple(
                item.evidence for item in (*ci, *scripts, *docs)
            ))
            gates = self._validate_interpreted(root, interpreted)
        evidence = tuple(
            value for plan in (*bootstrap, *gates, *excluded) for value in plan.source_evidence
        )
        ambiguities: list[str] = []
        if not gates:
            ambiguities.append("não foi possível provar um comando de validação local")
        if rejected and not gates:
            ambiguities.append("comandos candidatos exigiam interpretação de shell e foram recusados")
        components = self._components((*bootstrap, *gates))
        observed = tuple(sorted({plan.argv[0] for plan in (*bootstrap, *gates)}))
        return ProjectContract(
            repository_root=str(root),
            repository_identity=repository_identity,
            base_branch=base_branch,
            pull_request_target=pull_request_target,
            protected_branches=protected_branches,
            layout="monorepo" if len(components) > 1 else "single-package",
            observed_toolchains=observed,
            observed_build_systems=observed,
            bootstrap=tuple(bootstrap),
            gates=tuple(gates),
            expected_ci=tuple(dict.fromkeys(ci_names)),
            components=components,
            evidence=evidence,
            confidence=ContractConfidence.PROVEN if gates else ContractConfidence.AMBIGUOUS,
            ambiguities=tuple(ambiguities),
            excluded_operations=tuple(excluded),
        )

    def _workflow_candidates(self, root: Path) -> tuple[list[_Candidate], list[str]]:
        candidates: list[_Candidate] = []
        jobs: list[str] = []
        directory = root / ".github" / "workflows"
        for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            if self._workflow_applies_to_pull_request(lines):
                jobs.extend(self._workflow_job_names(lines))
            step_name = path.stem
            cwd = "."
            number = 0
            while number < len(lines):
                line = lines[number]
                number += 1
                stripped = line.strip()
                if stripped == "jobs:":
                    continue
                name = re.match(r"^-?\s*name:\s*[\"']?(.+?)[\"']?\s*$", stripped)
                if name:
                    step_name = name.group(1)
                    cwd = "."
                working = re.match(r"working-directory:\s*[\"']?(.+?)[\"']?\s*$", stripped)
                if working:
                    cwd = working.group(1)
                run = re.match(r"-?\s*run:\s*[\"']?(.+?)[\"']?\s*$", stripped)
                if not run:
                    continue
                value = run.group(1)
                commands: list[tuple[str, int]] = []
                if value in {"|", ">", "|-", ">-"}:
                    run_indent = len(line) - len(line.lstrip())
                    while number < len(lines):
                        block_line = lines[number]
                        block_indent = len(block_line) - len(block_line.lstrip())
                        if block_line.strip() and block_indent <= run_indent:
                            break
                        number += 1
                        command = block_line.strip()
                        if command and not command.startswith("#"):
                            commands.append((command, number))
                    if value.startswith(">") and commands:
                        commands = [(
                            " ".join(command for command, _ in commands),
                            commands[0][1],
                        )]
                else:
                    commands.append((value, number))
                for command, command_line in commands:
                    semantic_evidence = f"{step_name} {command}"
                    if not any(
                        pattern.search(semantic_evidence)
                        for pattern in (_VALIDATION_WORDS, _BOOTSTRAP_WORDS, _REMOTE_WORDS)
                    ):
                        continue
                    candidates.append(_Candidate(step_name, command, cwd, SourceEvidence(
                        path.relative_to(root).as_posix(), "official_ci", command[:300], command_line
                    )))
        return candidates, jobs

    @staticmethod
    def _workflow_applies_to_pull_request(lines: list[str]) -> bool:
        """Considera checks apenas de workflows que podem observar um Pull Request."""
        jobs_start = next(
            (index for index, line in enumerate(lines) if line.strip() == "jobs:"),
            len(lines),
        )
        header = lines[:jobs_start]
        on_start = next(
            (index for index, line in enumerate(header) if re.match(r"^on\s*:", line)),
            None,
        )
        if on_start is None:
            return True
        triggers = "\n".join(header[on_start:])
        return re.search(
            r"(?m)(?:^\s*|[\[, ]\s*)(pull_request(?:_target)?|merge_group)(?:\s*:|\s*[,\]])",
            triggers,
        ) is not None

    @staticmethod
    def _workflow_job_names(lines: list[str]) -> list[str]:
        """Extrai nomes observáveis de jobs, inclusive matrizes simples."""
        result: list[str] = []
        jobs_start = next((index for index, line in enumerate(lines) if line.strip() == "jobs:"), None)
        if jobs_start is None:
            return result
        jobs_end = next(
            (
                index for index in range(jobs_start + 1, len(lines))
                if lines[index].strip() and not lines[index].startswith((" ", "\t"))
            ),
            len(lines),
        )
        starts = [
            (index, match.group(1))
            for index, line in enumerate(lines[jobs_start + 1 : jobs_end], jobs_start + 1)
            if (match := re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line))
        ]
        for position, (start, identifier) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            segment = lines[start + 1 : end]
            display = next(
                (
                    match.group(1).strip().strip("\"'")
                    for line in segment
                    if (match := re.match(r"^    name:\s*(.+?)\s*$", line))
                ),
                None,
            )
            if display and "${{" not in display:
                result.append(display)
                continue
            matrix_key = None
            if display:
                match = re.fullmatch(r"\$\{\{\s*matrix\.([A-Za-z0-9_.-]+)\s*\}\}", display)
                matrix_key = match.group(1) if match else None
            if matrix_key:
                values = [
                    match.group(1).strip().strip("\"'")
                    for line in segment
                    if (len(line) - len(line.lstrip())) >= 8
                    and (match := re.match(rf"^\s+(?:-\s*)?{re.escape(matrix_key)}:\s*(.+?)\s*$", line))
                    and "${{" not in match.group(1)
                ]
                if values:
                    result.extend(values)
                    continue
            result.append(identifier)
        return result

    def _script_candidates(self, root: Path) -> list[_Candidate]:
        result: list[_Candidate] = []
        for path in self._files(root):
            if path.suffix.lower() != ".json" or path.stat().st_size > 1_000_000:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if not isinstance(scripts, dict):
                continue
            for name, value in scripts.items():
                if isinstance(value, str) and _VALIDATION_WORDS.search(str(name)):
                    result.append(_Candidate(str(name), value, path.parent.relative_to(root).as_posix() or ".", SourceEvidence(
                        path.relative_to(root).as_posix(), "versioned_script", str(name)
                    )))
        return result

    def _documentation_candidates(self, root: Path) -> list[_Candidate]:
        result: list[_Candidate] = []
        names = {"agents.md", "contributing.md", "readme.md"}
        for path in self._files(root):
            relative = path.relative_to(root)
            if path.name.casefold() not in names and "docs" not in {part.casefold() for part in relative.parts}:
                continue
            if path.suffix.casefold() not in {".md", ".txt"} or path.stat().st_size > 500_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for number, line in enumerate(lines, 1):
                if not _VALIDATION_WORDS.search(line):
                    continue
                for command in re.findall(r"`([^`]{3,300})`", line):
                    result.append(_Candidate("validação documentada", command, ".", SourceEvidence(
                        relative.as_posix(), "operational_documentation", line.strip()[:300], number
                    )))
        return result

    def _select(self, ci: list[_Candidate], scripts: list[_Candidate], docs: list[_Candidate]) -> list[_Candidate]:
        # CI oficial tem precedência. Documentação só promove comandos que também
        # aparecem em automação ou apontam para um executável versionado.
        if not ci:
            return [*scripts, *docs]
        selected = list(ci)
        ci_commands = {item.command.strip() for item in ci}
        selected.extend(item for item in scripts if item.command.strip() in ci_commands)
        selected.extend(item for item in docs if item.command.strip() in ci_commands)
        return selected

    def _to_plan(self, candidate: _Candidate) -> CommandPlan:
        risk = self._risk(candidate.name + " " + candidate.command)
        argv = self._parse_argv(candidate.command)
        capability = "bootstrap" if _BOOTSTRAP_WORDS.search(candidate.name) else self._capability(candidate.name)
        return CommandPlan(
            name=self._slug(candidate.name), capability=capability,
            display_name=candidate.name, argv=argv, cwd=candidate.cwd,
            timeout_seconds=900, source_evidence=(candidate.evidence,),
            confidence=1.0 if candidate.evidence.kind == "official_ci" else 0.8,
            risk_class=risk,
        )

    @staticmethod
    def _parse_argv(command: str) -> tuple[str, ...]:
        if _SHELL_OPERATORS.search(command):
            raise ContractResolutionError("comando com sintaxe de shell não pode virar gate estruturado")
        if re.search(r"(?i)(?:token|secret|password)\s*=|https?://[^/\s]+@", command):
            raise ContractResolutionError("comando contém credencial ou URL sensível")
        try:
            raw = shlex.split(command, posix=False)
            argv = tuple(
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}
                else value
                for value in raw
            )
        except ValueError as error:
            raise ContractResolutionError("comando versionado possui quoting inválido") from error
        if not argv or any("${{" in value or "${" in value or "%(" in value for value in argv):
            raise ContractResolutionError("comando depende de expansão dinâmica não comprovada")
        return argv

    @staticmethod
    def _risk(value: str) -> RiskClass:
        lowered = value.casefold()
        parts = lowered.split()
        if parts and Path(parts[0]).name in {
            "sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"
        } and any(part in {"-c", "/c", "-command", "-encodedcommand"} for part in parts[1:]):
            return RiskClass.UNKNOWN
        if _REMOTE_WORDS.search(value):
            return RiskClass.REMOTE_MUTATION
        if re.search(r"(?:^|\s)(rm|del|rmdir|remove-item|drop|destroy)(?:\s|$)", lowered):
            return RiskClass.DESTRUCTIVE
        return RiskClass.SAFE_LOCAL

    @staticmethod
    def _capability(name: str) -> str:
        match = _VALIDATION_WORDS.search(name)
        return match.group(1).casefold() if match else "project-validation"

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug[:80] or "project-validation"

    @staticmethod
    def _looks_versioned(command: str) -> bool:
        first = command.strip().split(maxsplit=1)[0].replace("\\", "/")
        return first.startswith(("./", "scripts/", "tools/"))

    @staticmethod
    def _deduplicate(plans: list[CommandPlan]) -> list[CommandPlan]:
        values: dict[tuple[tuple[str, ...], str], CommandPlan] = {}
        for plan in plans:
            key = (plan.argv, plan.cwd)
            previous = values.get(key)
            if previous is None:
                values[key] = plan
            else:
                values[key] = CommandPlan(**{
                    **previous.__dict__,
                    "source_evidence": tuple(dict.fromkeys((*previous.source_evidence, *plan.source_evidence))),
                    "confidence": max(previous.confidence, plan.confidence),
                })
        return list(values.values())

    @staticmethod
    def _components(plans: Iterable[CommandPlan]) -> tuple[ProjectComponent, ...]:
        paths = tuple(dict.fromkeys(plan.cwd for plan in plans)) or (".",)
        return tuple(ProjectComponent(Path(path).name or "root", path) for path in paths)

    def _files(self, root: Path) -> Iterable[Path]:
        if (root / ".git").exists():
            listed = self.runner.run(["git", "ls-files", "-z"], cwd=root)
            if listed.succeeded:
                for relative in listed.stdout.split("\x00"):
                    path = root / relative
                    if relative and path.is_file():
                        yield path
                return
        for path in root.rglob("*"):
            if path.is_file() and not any(
                part in _IGNORED_DIRS or part.startswith((".pytest", ".orch"))
                for part in path.relative_to(root).parts
            ):
                yield path

    @staticmethod
    def _validate_interpreted(root: Path, plans: tuple[CommandPlan, ...]) -> list[CommandPlan]:
        accepted: list[CommandPlan] = []
        for plan in plans:
            if (
                not plan.source_evidence
                or plan.risk_class is not RiskClass.SAFE_LOCAL
                or ProjectCapabilityResolver._risk(" ".join(plan.argv)) is not RiskClass.SAFE_LOCAL
            ):
                continue
            proven = True
            for source in plan.source_evidence:
                path = (root / source.path).resolve()
                if root not in path.parents or not path.is_file():
                    proven = False
                    break
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    proven = False
                    break
                if " ".join(plan.argv) not in content:
                    proven = False
                    break
            if proven:
                accepted.append(plan)
        return accepted


# Nome arquitetural alternativo útil para integrações externas.
RepositoryAnalyzer = ProjectCapabilityResolver
ExecutionPlanResolver = ProjectCapabilityResolver
