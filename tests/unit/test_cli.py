"""Testes da CLI."""

from pathlib import Path

from typer.testing import CliRunner

from ai_dev_orchestrator import __version__
from ai_dev_orchestrator.cli import app
from ai_dev_orchestrator.services.pipeline import RunResult
from ai_dev_orchestrator.services.resume import ResumeError, ResumeResult
from ai_dev_orchestrator.services.work import WorkResult
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.services.supersession import SupersessionPreview

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


def test_work_requires_no_issue_or_branch_and_delegates(monkeypatch) -> None:
    run_result = RunResult(42, "item", "work/acao", Path("worktree"), "origin/main",
                           "session", "fim", "Done")

    class Service:
        def work(self) -> WorkResult:
            return WorkResult(False, run=run_result)

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.WorkService.from_config", lambda _: Service())

    result = runner.invoke(app, ["work"])

    assert result.exit_code == 0
    assert "Issue selecionada: #42" in result.output
    assert "Branch: work/acao" in result.output


def test_work_reports_no_eligible_issue_as_success(monkeypatch) -> None:
    class Service:
        def work(self) -> None:
            return None

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.WorkService.from_config", lambda _: Service())

    result = runner.invoke(app, ["work"])

    assert result.exit_code == 0
    assert result.output == "Nenhuma Issue Ready elegível.\n"



def test_resume_encaminha_flags_explicitas_sem_substituir_identidade(monkeypatch):
    calls = []

    class Service:
        def resume(self, issue, **options):
            calls.append((issue, options))
            return ResumeResult(issue, "same", "TESTING", "feat/recovery", "session", 39, "a" * 40, 2)

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.ResumeService.from_config", lambda _: Service())
    for flag, option in (("--retry-provider", "retry_provider"), ("--recover-failed", "recover_failed")):
        result = runner.invoke(app, ["resume", "--issue", "37", flag])
        assert result.exit_code == 0
        assert calls[-1] == (37, {option: True})
        assert "Execução: same" in result.output


def test_supersede_yes_nao_pergunta_e_persiste_por_servico(monkeypatch):
    calls = []
    record = RunRecord("antiga", 45, ExecutionPhase.FAILED, __import__("datetime").datetime.now(), __import__("datetime").datetime.now(),
                       branch="work/antigo", pull_request_number=49, pull_request_url="url", current_head_sha="a" * 40)

    class Service:
        def preview(self, issue):
            calls.append(("preview", issue))
            return SupersessionPreview(record, "b" * 40, __import__("ai_dev_orchestrator.domain.recovery", fromlist=["PullRequestState"]).PullRequestState.CLOSED)
        def supersede(self, issue, reason):
            calls.append(("supersede", issue, reason))
            return record

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.SupersessionService.from_config", lambda _: Service())

    result = runner.invoke(app, ["supersede", "--issue", "45", "--reason", "PR encerrado", "--yes"])

    assert result.exit_code == 0
    assert calls == [("preview", 45), ("supersede", 45, "PR encerrado")]
    assert "histórico preservado" in result.output


def test_supersede_confirmacao_negativa_nao_altera_nada(monkeypatch):
    calls = []
    record = RunRecord("antiga", 45, ExecutionPhase.FAILED, __import__("datetime").datetime.now(), __import__("datetime").datetime.now(),
                       branch="work/antigo", pull_request_number=49, pull_request_url="url")

    class Service:
        def preview(self, issue):
            return SupersessionPreview(record, None, __import__("ai_dev_orchestrator.domain.recovery", fromlist=["PullRequestState"]).PullRequestState.CLOSED)
        def supersede(self, *args):
            calls.append(args)
            return record

    monkeypatch.setattr("ai_dev_orchestrator.cli.load_config", lambda: object())
    monkeypatch.setattr("ai_dev_orchestrator.cli.SupersessionService.from_config", lambda _: Service())

    result = runner.invoke(app, ["supersede", "--issue", "45", "--reason", "PR encerrado"], input="n\n")

    assert result.exit_code == 0 and calls == []
    assert "cancelada" in result.output
