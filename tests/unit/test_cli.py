"""Testes da CLI."""

from pathlib import Path

from typer.testing import CliRunner

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.services.pipeline import RunResult
from ai_dev_orchestrator.services.resume import ResumeError, ResumeResult

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


def test_resume_accepts_only_issue_and_displays_a_short_summary(monkeypatch) -> None:
    calls: list[int] = []

    class Service:
        def resume(self, issue: int) -> ResumeResult:
            calls.append(issue)
            return ResumeResult(37, "execution-37", "WAITING_CI", "feat/recovery",
                                "session-37", 39, "a" * 40, 2)

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config",
                        lambda _config: Service())

    result = runner.invoke(app, ["resume", "--issue", "37"])

    assert result.exit_code == 0
    assert calls == [37]
    assert "Execução: execution-37" in result.output
    assert "Fase: WAITING_CI" in result.output
    assert "Sessão Codex: session-37" in result.output
    assert "PR: #39" in result.output
    assert f"HEAD: {'a' * 40}" in result.output
    assert "Correções: 2" in result.output


def test_resume_has_no_branch_override() -> None:
    result = runner.invoke(app, ["resume", "--issue", "37", "--branch", "outra"])

    assert result.exit_code != 0


def test_resume_reports_controlled_error(monkeypatch) -> None:
    class Service:
        def resume(self, issue: int) -> ResumeResult:
            raise ResumeError("Nenhuma execução ativa")

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config",
                        lambda _config: Service())

    result = runner.invoke(app, ["resume", "--issue", "37"])

    assert result.exit_code == 1
    assert "Erro: Nenhuma execução ativa" in result.output
