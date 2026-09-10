"""Cenários agnósticos e de segurança do contrato operacional."""

from pathlib import Path

import pytest

from ai_dev_orchestrator.domain.project_contract import ContractConfidence, RiskClass
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.project_discovery import (
    AiProjectContractInterpreter,
    CONTRACT_COMMAND_SCHEMA,
    ProjectCapabilityResolver,
)
from ai_dev_orchestrator.services.validation import LocalValidationError, LocalValidationService
from ai_dev_orchestrator.domain.project import ProjectStatusOption, infer_status_mapping


def workflow(root: Path, body: str) -> None:
    path = root / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")


def resolve(root: Path):
    return ProjectCapabilityResolver().resolve(
        root,
        repository_identity="acme/example",
        base_branch="develop",
        pull_request_target="develop",
    )


def test_custom_tool_is_discovered_without_ecosystem_knowledge(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "validate.exe").touch()
    (tmp_path / "README.md").write_text("Valide com `tools/validate.exe --all`.", encoding="utf-8")
    workflow(tmp_path, "jobs:\n  quality:\n    steps:\n      - name: Validar\n        run: tools/validate.exe --all\n")

    contract = resolve(tmp_path)

    assert contract.confidence is ContractConfidence.PROVEN
    assert contract.gates[0].argv == ("tools/validate.exe", "--all")
    assert contract.gates[0].source_evidence[0].kind == "official_ci"


def test_monorepo_preserves_multiple_working_directories(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    workflow(tmp_path, """jobs:
  validate:
    steps:
      - name: Test frontend
        working-directory: frontend
        run: ./check --all
      - name: Test backend
        working-directory: backend
        run: ./verify --all
""")

    contract = resolve(tmp_path)

    assert contract.layout == "monorepo"
    assert {gate.cwd for gate in contract.gates} == {"frontend", "backend"}


def test_deploy_is_audited_but_never_becomes_local_gate(tmp_path: Path) -> None:
    workflow(tmp_path, """jobs:
  ci:
    steps:
      - name: Test
        run: ./validate
      - name: Deploy production
        run: ./deploy --production
""")

    contract = resolve(tmp_path)

    assert [gate.argv for gate in contract.gates] == [("./validate",)]
    assert contract.excluded_operations[0].risk_class is RiskClass.REMOTE_MUTATION


def test_folded_remote_migration_and_staging_smoke_never_become_local_gates(
    tmp_path: Path,
) -> None:
    workflow(tmp_path, """jobs:
  deploy:
    steps:
      - name: Apply migrations
        run: >-
          npx wrangler d1 migrations apply example-staging
          --remote --env staging
      - name: Smoke staging
        run: npm run smoke:staging
""")

    contract = resolve(tmp_path)

    assert contract.gates == ()
    assert len(contract.excluded_operations) == 2
    assert all(
        plan.risk_class is RiskClass.REMOTE_MUTATION
        for plan in contract.excluded_operations
    )


def test_dispatch_only_deploy_workflow_is_not_expected_pr_ci(tmp_path: Path) -> None:
    workflow(tmp_path, """name: Deploy
on:
  workflow_dispatch:
jobs:
  deploy:
    name: Deploy staging
    steps:
      - name: Deploy staging
        run: ./deploy --staging
""")

    contract = resolve(tmp_path)

    assert contract.expected_ci == ()
    assert contract.gates == ()
    assert len(contract.excluded_operations) == 1


def test_multiline_workflow_accepts_only_standalone_structured_commands(tmp_path: Path) -> None:
    workflow(tmp_path, """jobs:
  ci:
    steps:
      - name: Quality
        run: |
          ./lint --all
          ./test --unit
""")

    contract = resolve(tmp_path)

    assert [gate.argv for gate in contract.gates] == [
        ("./lint", "--all"), ("./test", "--unit")
    ]


def test_ci_names_follow_static_and_matrix_job_names(tmp_path: Path) -> None:
    workflow(tmp_path, """jobs:
  quality:
    name: Quality
    steps:
      - run: ./check
  tests:
    name: ${{ matrix.label }}
    strategy:
      matrix:
        include:
          - label: Linux tests
          - label: Windows tests
    steps:
      - run: ./test
""")

    assert resolve(tmp_path).expected_ci == ("Quality", "Linux tests", "Windows tests")


@pytest.mark.parametrize("command", ["./validate && rm -rf .", "./validate | evil", "./validate; del *", "$(evil)"])
def test_shell_injection_is_not_accepted(tmp_path: Path, command: str) -> None:
    workflow(tmp_path, f"jobs:\n  ci:\n    steps:\n      - name: Test\n        run: {command}\n")

    contract = resolve(tmp_path)

    assert contract.gates == ()
    assert contract.confidence is ContractConfidence.AMBIGUOUS


def test_validator_rejects_cwd_outside_worktree_before_process(tmp_path: Path) -> None:
    from ai_dev_orchestrator.domain.project_contract import CommandPlan

    with pytest.raises(ValueError, match="cwd"):
        CommandPlan("escape", "test", "Escape", ("tool",), "../outside")


def test_validator_uses_structured_argv_and_gate_cwd(tmp_path: Path) -> None:
    component = tmp_path / "component"
    component.mkdir()
    calls: list[tuple[tuple[str, ...], Path]] = []

    class Runner:
        def run(self, arguments, cwd=None):
            calls.append((tuple(arguments), cwd))
            return CommandResult(0)

    from ai_dev_orchestrator.domain.project_contract import CommandPlan
    plans = (CommandPlan("custom", "test", "Custom", ("tool", "--all"), "component"),)

    result = LocalValidationService(Runner(), plans).validate(tmp_path)

    assert result[0].succeeded
    assert calls == [(('tool', '--all'), component.resolve())]


def test_failed_custom_gate_keeps_sanitized_short_diagnostic(tmp_path: Path) -> None:
    class Runner:
        def run(self, arguments, cwd=None):
            return CommandResult(7, stderr="falha")

    from ai_dev_orchestrator.domain.project_contract import CommandPlan
    with pytest.raises(LocalValidationError, match="custom.*falha"):
        LocalValidationService(
            Runner(), (CommandPlan("custom", "build", "Custom", ("tool",)),)
        ).validate(tmp_path)


def test_simple_project_statuses_map_to_logical_states_without_view_dependency() -> None:
    mapping = infer_status_mapping((
        ProjectStatusOption("1", "Todo"),
        ProjectStatusOption("2", "Em andamento"),
        ProjectStatusOption("3", "Finalizado"),
    ))

    assert mapping is not None
    assert mapping["ready"] == "Todo"
    assert mapping["ai_review"] == "Em andamento"
    assert mapping["completed"] == "Finalizado"


def test_ambiguous_project_statuses_fail_closed() -> None:
    assert infer_status_mapping((
        ProjectStatusOption("1", "Fila A"),
        ProjectStatusOption("2", "Fila B"),
        ProjectStatusOption("3", "Fim"),
    )) is None


def test_local_gate_failure_resumes_same_session_with_independent_budget(tmp_path: Path) -> None:
    from ai_dev_orchestrator.adapters.codex import CodexExecution
    from ai_dev_orchestrator.config import OrchestratorConfig
    from ai_dev_orchestrator.domain.execution import ExecutionPhase
    from ai_dev_orchestrator.domain.worktree import GitWorktree
    from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
    from ai_dev_orchestrator.services.pipeline import RunPipeline
    from ai_dev_orchestrator.services.validation import GateResult
    from ai_dev_orchestrator.domain.issue import Issue

    config = OrchestratorConfig(
        github={"owner": "acme", "repository": "repo", "project_number": 1, "ready_status": "Ready"},
        execution={"max_attempts": 1, "max_parallel_runs": 1, "auto_merge": False},
        workspace={
            "repository_path": tmp_path, "worktrees_dir": tmp_path / "worktrees", "base_ref": "main",
        },
    )
    failed = GateResult("quality", ("tool",), False, 1, "teste falhou")

    class Validator:
        calls = 0

        def validate(self, _path):
            self.calls += 1
            if self.calls == 1:
                raise LocalValidationError("quality falhou", result=failed)
            return (GateResult("quality", ("tool",), True, 0, ""),)

    class Codex:
        sessions: list[str] = []

        def resume(self, _path, session_id, _prompt):
            self.sessions.append(session_id)
            return CodexExecution(session_id, "corrigido", "", "", True)

    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(78, branch="work/generic", worktree_path=str(tmp_path), base_ref="main")
    run = store.transition(run.id, ExecutionPhase.CODEX_RUNNING, summary="codex")
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="gates", codex_session_id="same-session")
    pipeline = RunPipeline(config, object(), object(), object(), object(), Codex(), Validator(), execution_store=store)
    pipeline._execution_id = run.id

    gates, message = pipeline._validate_with_recovery(
        Issue(78, "T", "", "OPEN", "url", (), ()),
        GitWorktree(tmp_path, tmp_path, "work/generic", "main"),
        "same-session",
        "inicial",
    )

    persisted = store.get(run.id)
    assert message == "corrigido" and gates[0].succeeded
    assert persisted.local_gate_correction_attempts == 1
    assert persisted.ci_correction_attempts == persisted.correction_attempts == 0
    assert pipeline.codex_executor.sessions == ["same-session"]


def test_ai_interpretation_remains_bound_to_repository_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Run validation with `oddtool --all`.", encoding="utf-8")

    class Invoker:
        def invoke(self, _prompt, cwd, schema):
            assert cwd == tmp_path and schema == CONTRACT_COMMAND_SCHEMA
            return """[{"capability":"project-validation","display_name":"Official validation",
            "argv":["oddtool","--all"],"cwd":".","timeout_seconds":60,
            "source_evidence":[{"path":"README.md","kind":"operational_documentation",
            "detail":"documented","line":1}],"confidence":0.9,"risk_class":"SAFE_LOCAL"}]"""

    contract = ProjectCapabilityResolver(
        AiProjectContractInterpreter(Invoker(), tmp_path)
    ).resolve(
        tmp_path, repository_identity="acme/custom", base_branch="main",
        pull_request_target="main",
    )

    assert contract.confidence is ContractConfidence.PROVEN
    assert contract.gates[0].argv == ("oddtool", "--all")

    from ai_dev_orchestrator.cli import _gate_overrides_from_contract

    assert _gate_overrides_from_contract(contract) == [{
        "name": contract.gates[0].name,
        "capability": "project-validation",
        "argv": ("oddtool", "--all"),
        "cwd": ".",
        "timeout_seconds": 60,
        "required": True,
    }]


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest",
        "npm run test",
        "dotnet test",
        "./gradlew test",
        "./mvnw verify",
        "go test ./...",
        "cargo test",
    ],
)
def test_same_resolver_builds_structured_plan_for_multiple_ecosystems(
    tmp_path: Path, command: str
) -> None:
    workflow(
        tmp_path,
        f"jobs:\n  validation:\n    steps:\n      - name: Test\n        run: {command}\n",
    )

    contract = resolve(tmp_path)

    assert contract.confidence is ContractConfidence.PROVEN
    assert contract.gates[0].argv == ProjectCapabilityResolver._parse_argv(command)


def test_documented_command_is_evidence_without_versioned_ci(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "Execute os testes com `pytest -q`.", encoding="utf-8"
    )

    contract = resolve(tmp_path)

    assert contract.gates[0].argv == ("pytest", "-q")
    assert contract.gates[0].source_evidence[0].kind == "operational_documentation"


def test_versioned_script_is_evidence_without_versioned_ci(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"custom-runner --all"}}', encoding="utf-8"
    )

    contract = resolve(tmp_path)

    assert contract.gates[0].argv == ("custom-runner", "--all")
    assert contract.gates[0].source_evidence[0].kind == "versioned_script"


def test_official_ci_wins_over_contradictory_documentation(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "Execute os testes antigos com `./old-check`.", encoding="utf-8"
    )
    workflow(
        tmp_path,
        "jobs:\n  validation:\n    steps:\n      - name: Test\n        run: ./current-check\n",
    )

    contract = resolve(tmp_path)

    assert [gate.argv for gate in contract.gates] == [("./current-check",)]


def test_windows_paths_keep_backslashes_in_structured_argv() -> None:
    assert ProjectCapabilityResolver._parse_argv(
        r'tools\validate.exe --all'
    ) == (r"tools\validate.exe", "--all")
    assert ProjectCapabilityResolver._parse_argv(
        r'".\tools dir\validate.exe" --all'
    ) == (r".\tools dir\validate.exe", "--all")


def test_fingerprint_ignores_cosmetic_evidence_location_changes(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Teste com `custom-check --all`.", encoding="utf-8")
    first = resolve(tmp_path)
    readme.write_text(
        "Introducao atualizada.\n\nTeste com `custom-check --all`.", encoding="utf-8"
    )
    cosmetic = resolve(tmp_path)
    readme.write_text("Teste com `custom-check --strict`.", encoding="utf-8")
    operational = resolve(tmp_path)

    assert cosmetic.fingerprint == first.fingerprint
    assert operational.fingerprint != first.fingerprint
