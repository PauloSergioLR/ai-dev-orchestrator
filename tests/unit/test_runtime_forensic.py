"""Regressões sintéticas da auditoria de processos, contratos e limites."""

import json
import os
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexProviderFailure
from ai_dev_orchestrator.config import (
    CiConfig, ConvergenceConfig, ProviderConfig, ReviewConfig, SupervisorConfig,
)
from ai_dev_orchestrator.domain.project_contract import CommandPlan, RiskClass
from ai_dev_orchestrator.domain.provider import ProviderFailureKind, classify_process_failure
from ai_dev_orchestrator.infrastructure.process import (
    CommandResult, CommandRunner, ProcessFailureKind, resolve_executable,
)
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver
from ai_dev_orchestrator.services.validation import LocalFailureKind, LocalValidationError, LocalValidationService


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 0, -1])
def test_runtime_and_command_plan_reject_nonfinite_limits(value):
    with pytest.raises(ValueError):
        CommandRunner(timeout=value)
    with pytest.raises(ValueError):
        CommandRunner(idle_timeout=value)
    with pytest.raises(ValueError):
        CommandPlan("test", "test", "Teste", ("python",), timeout_seconds=value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), -0.1, 1.1])
def test_contract_confidence_is_finite_and_bounded(value):
    with pytest.raises(ValueError):
        CommandPlan("test", "test", "Teste", ("python",), confidence=value)


@pytest.mark.parametrize("model,field", [
    (CiConfig, "timeout_seconds"), (ConvergenceConfig, "poll_interval_seconds"),
    (ProviderConfig, "codex_timeout_seconds"), (ProviderConfig, "codex_idle_timeout_seconds"),
    (ProviderConfig, "codex_heartbeat_seconds"), (ReviewConfig, "timeout_seconds"),
    (SupervisorConfig, "max_sleep_seconds"), (SupervisorConfig, "retry_without_reset_seconds"),
])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_configuration_rejects_nonfinite_duration(model, field, value):
    with pytest.raises(ValidationError):
        model(**{field: value})


def test_codex_has_configurable_finite_limits():
    values = ProviderConfig(codex_timeout_seconds=3600, codex_idle_timeout_seconds=90)
    adapter = CodexAdapter(timeout=values.codex_timeout_seconds, idle_timeout=values.codex_idle_timeout_seconds)
    assert adapter.runner.timeout == 3600
    assert adapter.runner.idle_timeout == 90
    assert ProviderConfig().codex_timeout_seconds == 7200
    with pytest.raises(ValidationError):
        ProviderConfig(codex_timeout_seconds=86401)


def test_executable_resolves_relative_to_child_cwd_and_path(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    name = "probe.cmd" if os.name == "nt" else "probe"
    for directory in (parent, child):
        target = directory / name
        target.write_text("@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n")
        target.chmod(0o755)
    monkeypatch.chdir(parent)
    assert Path(resolve_executable("./" + name, child, {"PATH": ""})) == child / name
    assert Path(resolve_executable(name, child, {"PATH": "."})) == child / name
    assert resolve_executable(name, child, {"PATH": ""}) is None
    (child / name).unlink()
    assert resolve_executable(name, child, {"PATH": "."}) is None


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT e batch são específicos do Windows")
def test_windows_child_pathext_controls_resolution(tmp_path):
    (tmp_path / "probe.cmd").write_text("@echo ok\n")
    assert resolve_executable("probe", tmp_path, {"Path": ".", "Pathext": ".CMD"})
    assert resolve_executable("probe", tmp_path, {"PATH": ".", "PATHEXT": ".EXE"}) is None


@pytest.mark.skipif(os.name != "nt", reason="Batch é específico do Windows")
@pytest.mark.parametrize("payload", ["& echo danger", "| echo danger", "> marker", "%PATH%", "!PATH!", '" & echo danger', "one\ntwo", "^& echo danger"])
def test_batch_shell_metacharacters_never_reach_process(tmp_path, payload):
    script = tmp_path / "validate.cmd"
    marker = tmp_path / "launched"
    script.write_text(f'@echo launched > "{marker}"\n', encoding="utf-8")
    result = CommandRunner().run([str(script), payload])
    assert result.failure_kind is ProcessFailureKind.UNSAFE_COMMAND
    assert classify_process_failure(result.failure_kind) is ProviderFailureKind.PROTOCOL_ERROR
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Batch é específico do Windows")
def test_batch_simple_arguments_and_spaces_keep_working(tmp_path):
    directory = tmp_path / "com espaços"
    directory.mkdir()
    script = directory / "validate.cmd"
    script.write_text("@echo %~1\n", encoding="utf-8")
    result = CommandRunner(timeout=10).run([str(script), "argumento com espacos"])
    assert result.succeeded
    assert result.stdout.strip() == "argumento com espacos"


def test_idle_timeout_keeps_output_and_stops_silent_process():
    runner = CommandRunner(timeout=10, idle_timeout=0.5)
    result = runner.run([sys.executable, "-c", "import time; print('parcial',flush=True); time.sleep(20)"])
    assert result.failure_kind is ProcessFailureKind.TIMEOUT
    assert result.stdout.strip() == "parcial"
    assert "sem saída" in result.error
    assert runner.activity.snapshot()[0] > 0


def test_output_activity_resets_idle_but_never_extends_total_timeout():
    runner = CommandRunner(timeout=1, idle_timeout=0.4)
    result = runner.run([sys.executable, "-c", "import time\nfor _ in range(100):\n print('ativo',flush=True)\n time.sleep(.05)"])
    assert result.failure_kind is ProcessFailureKind.TIMEOUT
    assert result.stdout.count("ativo") > 2
    assert "timeout de 1s" in result.error


def test_output_capture_is_bounded_and_fails_closed(monkeypatch):
    monkeypatch.setattr("ai_dev_orchestrator.infrastructure.process.MAX_CAPTURE_BYTES", 1024)
    result = CommandRunner(timeout=10).run([sys.executable, "-c", "print('x'*100000)"])
    assert result.failure_kind is ProcessFailureKind.OUTPUT_LIMIT
    assert len(result.stdout_bytes) + len(result.stderr_bytes) <= 1024
    assert classify_process_failure(result.failure_kind) is ProviderFailureKind.PROTOCOL_ERROR


def test_native_argv_does_not_interpret_shell_metacharacters():
    payload = '&|<>^%!"() $($env:PATH)'
    result = CommandRunner(timeout=10).run([sys.executable, "-c", "import sys; print(sys.argv[1])", payload])
    assert result.succeeded and result.stdout.strip() == payload


def test_python_gate_environment_isolated_without_changing_parent_or_other_stacks(monkeypatch, tmp_path):
    names = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTHONPYCACHEPREFIX", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "PYTEST_DEBUG_TEMPROOT", "TMP", "TEMP")
    for name in names:
        monkeypatch.setenv(name, str(tmp_path / "foreign"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "foreign-venv"))
    source = "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(names) + "}))"
    environment = LocalValidationService._gate_environment((sys.executable, "-c", source), str(tmp_path))
    result = CommandRunner(timeout=10).run([sys.executable, "-c", source], environment=environment)
    assert result.succeeded
    observed = json.loads(result.stdout)
    for name in names:
        assert observed[name] == (str(tmp_path) if name in {"TMP", "TEMP", "PYTEST_DEBUG_TEMPROOT"} else None)
        assert os.environ[name] == str(tmp_path / "foreign")
    assert LocalValidationService._gate_environment(("npm", "test"), str(tmp_path)) is None
    assert "VIRTUAL_ENV" not in environment


@pytest.mark.parametrize("command", [
    'python -c "print(123)"',
    "'python -c \"print(123)\"'",
    '"python -c \\"print(123)\\""',
])
def test_workflow_yaml_preserves_python_c_quoting(tmp_path, command):
    workflow = tmp_path / ".github/workflows/check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  test:\n    steps:\n      - name: Test\n        run: " + command + "\n")
    contract = ProjectCapabilityResolver().resolve(tmp_path, repository_identity="a/b", base_branch="main", pull_request_target="main")
    assert contract.gates[0].argv == ("python", "-c", "print(123)")


@pytest.mark.parametrize("command", ['bash -c "echo test"', 'cmd /c "echo test"', 'pwsh -Command "Write-Output test"', "'\"C:\\tools\\powershell.exe\" -c \"test\"'"])
def test_safe_step_name_cannot_hide_shell_executable(tmp_path, command):
    workflow = tmp_path / ".github/workflows/check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  test:\n    steps:\n      - name: Test\n        run: " + command + "\n")
    contract = ProjectCapabilityResolver().resolve(tmp_path, repository_identity="a/b", base_branch="main", pull_request_target="main")
    assert not contract.gates
    assert contract.excluded_operations[0].risk_class is RiskClass.UNKNOWN


def test_codex_cannot_reuse_previous_terminal_for_incomplete_new_turn(tmp_path):
    class Runner:
        def run(self, *args, **kwargs):
            return CommandResult(0, '\n'.join([
                '{"type":"thread.started","thread_id":"thread-safe"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"anterior"}}',
                '{"type":"turn.completed"}', '{"type":"turn.started"}',
            ]))

    with pytest.raises(CodexProviderFailure) as failure:
        CodexAdapter(Runner()).execute(tmp_path, "continuar")
    assert failure.value.classification is ProviderFailureKind.PROTOCOL_ERROR
    assert failure.value.session_id == "thread-safe"


def test_temp_indisponivel_nao_consume_correcao_codigo(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise PermissionError("temporário bloqueado")

    monkeypatch.setattr("ai_dev_orchestrator.services.validation.TemporaryDirectory", unavailable)
    service = LocalValidationService(plans=(CommandPlan("tests", "test", "Teste", (sys.executable, "-V")),))
    with pytest.raises(LocalValidationError) as failure:
        service.validate(tmp_path)
    assert failure.value.kind is LocalFailureKind.LOCAL_ENVIRONMENT_ERROR
    assert not failure.value.correctable


@pytest.mark.parametrize("prefix", ["", "log anterior\n" * 100])
def test_falha_permissao_pytest_cache_nao_e_falha_codigo(tmp_path, prefix):
    class Runner:
        def run(self, *_args, **_kwargs):
            return CommandResult(1, stderr=prefix + "PermissionError: .pytest_cache/v/cache/nodeids")

    service = LocalValidationService(Runner(), (CommandPlan("tests", "test", "Teste", ("pytest",)),))
    with pytest.raises(LocalValidationError) as failure:
        service.validate(tmp_path)
    assert failure.value.kind is LocalFailureKind.LOCAL_ENVIRONMENT_ERROR
    assert not failure.value.correctable


def test_cache_pytest_tem_raiz_temporaria_e_venv_do_worktree(tmp_path):
    scripts = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    environment = LocalValidationService._gate_environment(("uv", "run", "pytest"), str(tmp_path / "temp"), tmp_path)
    assert "cache_dir=" in environment["PYTEST_ADDOPTS"]
    assert environment["PATH"].split(os.pathsep)[0] == str(scripts)


def test_descoberta_recusa_evidencia_fora_da_raiz(tmp_path, monkeypatch):
    original = Path.resolve
    source = tmp_path / "package.json"
    source.write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")

    def escape(path, *args, **kwargs):
        return tmp_path.parent / "external.json" if path == source else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escape)
    from ai_dev_orchestrator.services.project_discovery import ContractResolutionError
    with pytest.raises(ContractResolutionError, match="fora do repositório"):
        ProjectCapabilityResolver().resolve(tmp_path, repository_identity="a/b", base_branch="main", pull_request_target="main")
