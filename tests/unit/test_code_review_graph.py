"""Cobertura da integração externa e fail-open com Code Review Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from ai_dev_orchestrator.config import CodeReviewGraphConfig
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.code_review_graph import CodeReviewGraphIntegrator


@dataclass
class FakeRunner:
    results: list[CommandResult]
    calls: list[tuple[list[str], Path | None]] = field(default_factory=list)

    def run(self, arguments, cwd=None, input_text=None, **kwargs):
        self.calls.append((list(arguments), cwd))
        return self.results.pop(0)


def result(payload: dict[str, object]) -> CommandResult:
    return CommandResult(0, json.dumps(payload))


def config() -> CodeReviewGraphConfig:
    return CodeReviewGraphConfig(
        enabled=True, command=("crg",), required_version="2.3.8"
    )


def test_first_use_builds_graph_and_reports_statistics(tmp_path: Path) -> None:
    runner = FakeRunner([
        CommandResult(0, "code-review-graph 2.3.8"),
        CommandResult(1, stderr="graph missing"),
        CommandResult(0, "Full build: 8 files, 21 nodes, 34 edges"),
        result({"nodes": 21, "edges": 34, "files": 8}),
    ])

    prepared = CodeReviewGraphIntegrator(config(), runner).prepare(tmp_path)

    assert prepared.used is True
    assert (prepared.action, prepared.nodes, prepared.edges) == ("build", 21, 34)
    assert runner.calls[2][0][1] == "build"


def test_next_use_updates_incrementally_and_captures_savings(tmp_path: Path) -> None:
    runner = FakeRunner([
        CommandResult(0, "code-review-graph 2.3.8"),
        result({"nodes": 20, "edges": 30, "files": 7}),
        CommandResult(0, "Incremental: 1 files updated\nEstimated savings: 420 tokens"),
        result({"nodes": 22, "edges": 35, "files": 8}),
    ])

    prepared = CodeReviewGraphIntegrator(config(), runner).prepare(tmp_path)

    assert prepared.used is True and prepared.action == "update"
    assert prepared.estimated_context_savings == "Estimated savings: 420 tokens"
    assert "--brief" in runner.calls[2][0]


def test_missing_or_incompatible_crg_falls_back_without_raising(tmp_path: Path) -> None:
    missing = CodeReviewGraphIntegrator(
        config(), FakeRunner([CommandResult(None, error="Executável não encontrado: crg")])
    ).prepare(tmp_path)
    incompatible = CodeReviewGraphIntegrator(
        config(), FakeRunner([CommandResult(0, "code-review-graph 2.2.0")])
    ).prepare(tmp_path)

    assert not missing.used and missing.action == "fallback"
    assert not incompatible.used and "incompatível" in (incompatible.warning or "")


def test_corrupt_status_after_build_falls_back(tmp_path: Path) -> None:
    runner = FakeRunner([
        CommandResult(0, "code-review-graph 2.3.8"),
        CommandResult(1),
        CommandResult(0, "build ok"),
        CommandResult(0, "not-json"),
    ])

    prepared = CodeReviewGraphIntegrator(config(), runner).prepare(tmp_path)

    assert not prepared.used
    assert "validado" in (prepared.warning or "")


def test_codex_overrides_pin_server_to_current_worktree(tmp_path: Path) -> None:
    overrides = CodeReviewGraphIntegrator(config()).codex_mcp_overrides(tmp_path)

    assert any('command="crg"' in item for item in overrides)
    assert any('args=["serve"]' in item for item in overrides)
    assert any(str(tmp_path.resolve()).replace("\\", "\\\\") in item for item in overrides)


def test_antigravity_mcp_is_merged_atomically_and_preserves_other_servers(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    path = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"existing": {
            "command": "existing", "args": ["serve"]
        }}}),
        encoding="utf-8",
    )

    configured = CodeReviewGraphIntegrator(config()).ensure_antigravity_mcp()
    data = json.loads(path.read_text(encoding="utf-8"))

    assert configured is True
    assert "existing" in data["mcpServers"]
    assert data["mcpServers"]["code-review-graph"] == {
        "command": "crg", "args": ["serve"]
    }


def test_invalid_antigravity_config_is_preserved_and_falls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    path = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    configured = CodeReviewGraphIntegrator(config()).ensure_antigravity_mcp()

    assert configured is False
    assert path.read_text(encoding="utf-8") == "not-json"
