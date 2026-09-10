"""Contrato operacional agnóstico descoberto a partir do repositório."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path


class ContractConfidence(StrEnum):
    PROVEN = "PROVEN"
    AMBIGUOUS = "AMBIGUOUS"


class RiskClass(StrEnum):
    SAFE_LOCAL = "SAFE_LOCAL"
    REMOTE_MUTATION = "REMOTE_MUTATION"
    DESTRUCTIVE = "DESTRUCTIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceEvidence:
    """Trecho rastreável que justifica uma decisão do contrato."""

    path: str
    kind: str
    detail: str
    line: int | None = None


@dataclass(frozen=True)
class CommandPlan:
    """Uma capacidade executável sem interpretação por shell."""

    name: str
    capability: str
    display_name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 900
    required: bool = True
    source_evidence: tuple[SourceEvidence, ...] = ()
    confidence: float = 1.0
    risk_class: RiskClass = RiskClass.SAFE_LOCAL
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.argv:
            raise ValueError("Gate precisa de nome e argv não vazio")
        if any(not isinstance(value, str) or not value or "\x00" in value for value in self.argv):
            raise ValueError("argv contém argumento inválido")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout do gate deve ser positivo")
        if Path(self.cwd).is_absolute() or ".." in Path(self.cwd).parts:
            raise ValueError("cwd do gate deve ser relativo e permanecer no worktree")


@dataclass(frozen=True)
class ProjectComponent:
    name: str
    path: str
    evidence: tuple[SourceEvidence, ...] = ()


@dataclass(frozen=True)
class ProjectContract:
    """Plano imutável consumido pelo control plane durante todo o run."""

    repository_root: str
    repository_identity: str
    base_branch: str
    pull_request_target: str
    protected_branches: tuple[str, ...]
    layout: str
    observed_toolchains: tuple[str, ...]
    observed_build_systems: tuple[str, ...]
    bootstrap: tuple[CommandPlan, ...]
    gates: tuple[CommandPlan, ...]
    expected_ci: tuple[str, ...]
    components: tuple[ProjectComponent, ...]
    evidence: tuple[SourceEvidence, ...]
    confidence: ContractConfidence
    ambiguities: tuple[str, ...] = ()
    excluded_operations: tuple[CommandPlan, ...] = ()
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            payload = self.to_dict(include_fingerprint=False)
            # O mesmo contrato em outro worktree precisa manter a identidade.
            payload["repository_root"] = "."
            digest = sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            object.__setattr__(self, "fingerprint", digest)

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload = asdict(self)
        if not include_fingerprint:
            payload.pop("fingerprint", None)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "ProjectContract":
        raw = json.loads(value)

        def evidence(items: list[dict[str, object]]) -> tuple[SourceEvidence, ...]:
            return tuple(SourceEvidence(**item) for item in items)

        def command(item: dict[str, object]) -> CommandPlan:
            data = dict(item)
            data["argv"] = tuple(data["argv"])
            data["depends_on"] = tuple(data.get("depends_on", ()))
            data["source_evidence"] = evidence(data.get("source_evidence", []))
            data["risk_class"] = RiskClass(data["risk_class"])
            return CommandPlan(**data)

        raw["protected_branches"] = tuple(raw["protected_branches"])
        raw["observed_toolchains"] = tuple(raw["observed_toolchains"])
        raw["observed_build_systems"] = tuple(raw["observed_build_systems"])
        raw["expected_ci"] = tuple(raw["expected_ci"])
        raw["bootstrap"] = tuple(command(item) for item in raw["bootstrap"])
        raw["gates"] = tuple(command(item) for item in raw["gates"])
        raw["excluded_operations"] = tuple(command(item) for item in raw.get("excluded_operations", []))
        raw["components"] = tuple(
            ProjectComponent(item["name"], item["path"], evidence(item.get("evidence", [])))
            for item in raw["components"]
        )
        raw["evidence"] = evidence(raw["evidence"])
        raw["ambiguities"] = tuple(raw.get("ambiguities", ()))
        raw["confidence"] = ContractConfidence(raw["confidence"])
        return cls(**raw)
