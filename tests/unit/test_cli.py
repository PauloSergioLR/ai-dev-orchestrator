"""Testes da CLI."""

from pathlib import Path

from typer.testing import CliRunner

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.services.pipeline import RunResult
from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.services.recovery import RecoveryError

runner = CliRunner()


def test_help_is_available() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Orquestrador local-first de desenvolvimento com IA." in result.output


def test_version_is_available() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"{__version__}\n"


def test_run_validates_required_options() -> None:
    assert runner.invoke(app, ["run", "--branch", "feat/test"]).exit_code != 0
    assert runner.invoke(app, ["run", "--issue", "0", "--branch", "feat/test"]).exit_code != 0
    assert runner.invoke(app, ["run", "--issue", "17"]).exit_code != 0


def test_run_delegates_to_pipeline_and_displays_summary(monkeypatch) -> None:
    calls: list[tuple[int, str]] = []

    class FakePipeline:
        def run(self, issue: int, branch: str) -> RunResult:
            calls.append((issue, branch))
            return RunResult(17, "item-17", branch, Path("C:/worktrees/feat--test"), "main",
                             "session-17", "Concluído", "In Progress")

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.RunPipeline.from_config", lambda config: FakePipeline())

    result = runner.invoke(app, ["run", "--issue", "17", "--branch", "feat/test"])

    assert result.exit_code == 0
    assert calls == [(17, "feat/test")]
    assert "Sessão Codex: session-17" in result.output


def test_resume_delegates_only_the_issue_and_shows_short_summary(monkeypatch) -> None:
    calls: list[int] = []

    class FakeService:
        def resume(self, issue: int):
            calls.append(issue)
            return type("Record", (), {"issue_number": issue, "phase": ExecutionPhase.TESTING})()

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config", lambda config: FakeService())

    result = runner.invoke(app, ["resume", "--issue", "37"])

    assert result.exit_code == 0
    assert calls == [37]
    assert result.output == "Issue #37 retomada em TESTING.\n"
    assert runner.invoke(app, ["resume", "--issue", "37", "--branch", "feat/x"]).exit_code != 0


def test_resume_without_active_execution_fails_without_traceback(monkeypatch) -> None:
    class FakeService:
        def resume(self, issue: int):
            raise RecoveryError("Não há execução ativa para a Issue #37")

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config", lambda config: FakeService())

    result = runner.invoke(app, ["resume", "--issue", "37"])

    assert result.exit_code == 1
    assert "execução ativa" in result.output
    assert "Traceback" not in result.output


def test_resume_terminal_execution_fails_without_traceback(monkeypatch) -> None:
    class FakeService:
        def resume(self, issue: int):
            raise RecoveryError("A execução da Issue #37 é terminal e não pode ser retomada")

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config", lambda config: FakeService())

    result = runner.invoke(app, ["resume", "--issue", "37"])

    assert result.exit_code == 1
    assert "terminal" in result.output
    assert "Traceback" not in result.output
