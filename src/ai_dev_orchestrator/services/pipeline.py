"""Coordena a execução de uma Issue sem ocultar mutações."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path, PureWindowsPath
import re
from typing import Protocol

from ai_dev_orchestrator.adapters.codex import CodexAdapter, CodexExecution
from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.adapters.publication import GitPublicationAdapter
from ai_dev_orchestrator.adapters.github import (
    GitHubIssueAdapter,
    GitHubProjectAdapter,
    GitHubProjectStatusAdapter,
    GitHubPullRequestAdapter,
    GitHubCiAdapter,
    PullRequest,
)
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.ci import CiStatus, StatusCheck
from ai_dev_orchestrator.domain.issue import Issue
from ai_dev_orchestrator.domain.project import ProjectItem, is_eligible_for_execution
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.services.validation import GateResult, LocalValidationService
from ai_dev_orchestrator.services.ci_gate import CiFailureError, CiGate, PullRequestCiReader
from ai_dev_orchestrator.services.convergence import (
    ConvergencePoller,
    ObservationDecision,
)
from ai_dev_orchestrator.services.review import (
    CorrectionContextBuilder,
    ContextBuilder,
    PullRequestReviewReader,
    REVIEW_PLAN_SCHEMA,
    STRUCTURED_REVIEW_SCHEMA,
    build_checklists,
    build_prompt,
    parse_review_plan,
    parse_structured_review,
)
from ai_dev_orchestrator.domain.review import (
    ReviewFinding,
    ReviewVerdict,
    StructuredReview,
)
from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.services.merge import (
    MergeGate,
    MergeGateError,
    MergePullRequestSnapshot,
    MergeResult,
    wait_for_merge_confirmation,
)
from ai_dev_orchestrator.domain.execution import ExecutionPhase, ExecutionStore
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.domain.provider import ProviderFailure
from ai_dev_orchestrator.domain.project_contract import CommandPlan, ProjectContract, SourceEvidence
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver
from ai_dev_orchestrator.services.validation import LocalValidationError
from ai_dev_orchestrator.services.code_review_graph import (
    CodeReviewGraphIntegrator,
    GRAPH_INSTRUCTION,
)


logger = logging.getLogger(__name__)


class RunPipelineError(Exception):
    """Indica em qual etapa a execução foi interrompida."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class IssueReader(Protocol):
    def get_issue(self, number: int) -> Issue: ...


class ProjectReader(Protocol):
    def list_items(self) -> tuple[ProjectItem, ...]: ...


class ProjectStatusWriter(Protocol):
    def set_status(self, project_item_id: str, status_name: str) -> None: ...


class WorktreeCreator(Protocol):
    def create_worktree(
        self,
        repository: str | Path,
        branch: str,
        worktree_path: str | Path,
        base_ref: str,
    ) -> GitWorktree: ...


class CodexExecutor(Protocol):
    def execute(self, worktree: str | Path, prompt: str) -> CodexExecution: ...
    def resume(
        self, worktree: str | Path, session_id: str, prompt: str
    ) -> CodexExecution: ...


class LocalValidator(Protocol):
    def validate(self, worktree: str | Path) -> tuple[GateResult, ...]: ...


class GitPublisher(Protocol):
    def commit(self, worktree: str | Path, issue_number: int) -> str: ...
    def push(self, worktree: str | Path, remote_name: str, branch: str) -> None: ...
    def commit_correction(self, worktree: str | Path) -> str: ...
    def current_head(self, worktree: str | Path) -> str: ...


class PullRequestCreator(Protocol):
    def create(
        self, issue: Issue, branch: str, gates: tuple[GateResult, ...]
    ) -> PullRequest: ...


class PullRequestMerger(Protocol):
    def get_merge_snapshot(
        self, pull_request_number: int
    ) -> MergePullRequestSnapshot: ...
    def merge(
        self, pull_request_number: int, expected_head_sha: str
    ) -> MergeResult: ...
    def verify_merge_commit(
        self, merge_commit_sha: str, merged_head_sha: str
    ) -> None: ...


@dataclass(frozen=True)
class RunResult:
    """Resultado imutável da execução e das revisões de uma Issue."""

    issue_number: int
    project_item_id: str
    branch: str
    worktree_path: Path
    base_ref: str
    session_id: str
    final_message: str
    project_status: str
    gates: tuple[GateResult, ...] = ()
    commit_sha: str = ""
    remote_name: str = ""
    pull_request_number: int = 0
    pull_request_url: str = ""
    pull_request_base: str = ""
    pull_request_head_sha: str = ""
    ci_checks: tuple[StatusCheck, ...] = ()
    ci_status: CiStatus | None = None
    review: StructuredReview | None = None
    blocking_severities: tuple[str, ...] = ()
    review_attempts: int = 0
    correction_attempts: int = 0
    final_reviewed_head_sha: str = ""
    prior_findings_count: int = 0
    auto_merge_enabled: bool = False
    merge_status: str = "NOT_REQUESTED"
    merged: bool = False
    merge_commit_sha: str = ""
    merged_head_sha: str = ""
    reviewed_head_sha: str = ""


def derive_worktree_path(worktrees_dir: Path, branch: str) -> Path:
    """Deriva um diretório seguro e determinístico sob a raiz configurada."""
    windows_branch = PureWindowsPath(branch)
    if not branch or Path(branch).is_absolute() or windows_branch.is_absolute():
        raise RunPipelineError(
            "A etapa de preparar o worktree recusou uma branch com caminho absoluto"
        )
    parts = re.split(r"[\\\\/]", branch)
    if any(part in {"", ".", ".."} for part in parts):
        raise RunPipelineError(
            "A etapa de preparar o worktree recusou uma branch com caminho inseguro"
        )
    directory_name = "--".join(re.sub(r"[^A-Za-z0-9._-]", "-", part) for part in parts)
    root = worktrees_dir.resolve()
    candidate = (root / directory_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RunPipelineError(
            "A etapa de preparar o worktree gerou um caminho fora de worktrees_dir"
        ) from error
    return candidate


def build_initial_prompt(issue: Issue, *, use_code_review_graph: bool = False) -> str:
    """Monta o prompt inicial de forma determinística, sem consultar providers."""
    return (
        f"Implemente a Issue #{issue.number}: {issue.title}\n\n"
        "Body completo da Issue:\n"
        f"{issue.body}\n\n"
        "Você está executando dentro do worktree já preparado para esta Issue. "
        "Leia e respeite o AGENTS.md do repositório/worktree. Trabalhe somente no escopo "
        "desta Issue. Nesta etapa, não faça commit, push, Pull Request ou merge. "
        "Execute as validações pedidas pela própria Issue quando aplicável."
        + (f"\n\n{GRAPH_INSTRUCTION}" if use_code_review_graph else "")
    )


class RunPipeline:
    """Executa as etapas ordenadas de preparação e início de uma sessão Codex."""

    def __init__(
        self,
        config: OrchestratorConfig,
        issue_reader: IssueReader,
        project_reader: ProjectReader,
        status_writer: ProjectStatusWriter,
        worktree_creator: WorktreeCreator,
        codex_executor: CodexExecutor,
        local_validator: LocalValidator | None = None,
        git_publisher: GitPublisher | None = None,
        pull_request_creator: PullRequestCreator | None = None,
        ci_reader: PullRequestCiReader | None = None,
        review_reader: PullRequestReviewReader | None = None,
        reviewer: AntigravityAdapter | None = None,
        pull_request_merger: PullRequestMerger | None = None,
        execution_store: ExecutionStore | None = None,
        convergence: ConvergencePoller | None = None,
        project_contract: ProjectContract | None = None,
        graph_integrator: CodeReviewGraphIntegrator | None = None,
    ) -> None:
        self.config = config
        self.issue_reader = issue_reader
        self.project_reader = project_reader
        self.status_writer = status_writer
        self.worktree_creator = worktree_creator
        self.codex_executor = codex_executor
        self.local_validator = local_validator
        self.git_publisher = git_publisher
        self.pull_request_creator = pull_request_creator
        self.ci_reader = ci_reader
        self.review_reader = review_reader
        self.reviewer = reviewer
        self.pull_request_merger = pull_request_merger
        self.execution_store = execution_store
        self.convergence = convergence or ConvergencePoller(config.convergence)
        self.project_contract = project_contract
        self.graph_integrator = graph_integrator or CodeReviewGraphIntegrator(
            config.code_review_graph
        )
        self._execution_id: str | None = None

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> RunPipeline:
        pull_requests = GitHubPullRequestAdapter(config)
        overrides = tuple(
            CommandPlan(
                gate.name,
                gate.capability,
                gate.name,
                gate.argv,
                gate.cwd,
                gate.timeout_seconds,
                gate.required,
                (SourceEvidence("orchestrator.toml", "explicit_override", gate.name),),
                1.0,
                ProjectCapabilityResolver._risk(" ".join(gate.argv)),
            )
            for gate in config.project.gates
        )
        contract = ProjectCapabilityResolver().resolve(
            config.workspace.repository_path,
            repository_identity=config.github.repository_full_name,
            base_branch=config.workspace.base_ref,
            pull_request_target=config.github.pull_request_base,
            protected_branches=config.github.protected_branches,
            overrides=overrides,
        )
        if contract.ambiguities:
            raise RunPipelineError("Contrato do projeto ambíguo: " + "; ".join(contract.ambiguities))
        return cls(
            config,
            GitHubIssueAdapter(config),
            GitHubProjectAdapter(config),
            GitHubProjectStatusAdapter(config),
            GitWorktreeAdapter(),
            CodexAdapter(
                model=config.providers.codex_model,
                code_review_graph_command=(
                    config.code_review_graph.command
                    if config.code_review_graph.enabled else ()
                ),
            ),
            LocalValidationService(),
            GitPublicationAdapter(),
            pull_requests,
            GitHubCiAdapter(config),
            pull_requests,
            AntigravityAdapter(
                config.review.timeout_seconds, model=config.providers.gemini_model,
                executable=config.review.executable,
            ),
            pull_requests,
            SqliteExecutionStore(config.state.database_path),
            project_contract=contract,
            graph_integrator=CodeReviewGraphIntegrator(config.code_review_graph),
        )

    def run(self, issue_number: int, branch: str, *, base_ref: str | None = None) -> RunResult:
        self._execution_id = None
        try:
            return self._run(issue_number, branch, base_ref=base_ref)
        except CiFailureError as error:
            raise RunPipelineError(
                f"CI reprovada para a Issue #{issue_number}; a recuperação automática manterá "
                f"o Status em '{self.config.github.status_for('ai_review')}' e retomará a mesma sessão Codex",
                reason="CI_FAILURE_RECOVERY",
            ) from error
        except Exception as error:
            if (
                self.execution_store is not None
                and self._execution_id is not None
                and getattr(error, "reason", None) != "CI_FAILURE_RECOVERY"
            ):
                from ai_dev_orchestrator.services.escalation import EscalationService
                EscalationService(self.config, self.execution_store, self.status_writer).assess(
                    self.execution_store.get(self._execution_id), error=error
                )
            raise

    def _run(
        self, issue_number: int, branch: str, *, base_ref: str | None = None
    ) -> RunResult:
        if issue_number <= 0:
            raise RunPipelineError("A Issue deve ser um inteiro positivo")
        if branch in self.config.github.protected_branches:
            raise RunPipelineError(
                f"A branch de trabalho '{branch}' é protegida pela configuração"
            )
        try:
            issue = self.issue_reader.get_issue(issue_number)
            if issue.state != "OPEN":
                raise RunPipelineError(
                    f"A etapa de validar elegibilidade falhou: a Issue #{issue_number} não está OPEN"
                )
            item = self._find_project_item(issue_number)
            if not is_eligible_for_execution(
                item,
                self.config.github.repository_full_name,
                self.config.github.status_for("ready"),
            ):
                raise RunPipelineError(
                    f"A etapa de validar elegibilidade falhou: a Issue #{issue_number} não está em "
                    f"'{self.config.github.status_for('ready')}' no repositório configurado"
                )
            selected_base_ref = base_ref or self.config.workspace.base_ref
            worktree_path = derive_worktree_path(
                self.config.workspace.worktrees_dir, branch
            )
        except RunPipelineError:
            raise
        except Exception as error:
            raise RunPipelineError(
                f"Falha antes de criar o worktree: {error}"
            ) from error
        if self.execution_store is not None:
            try:
                record = self.execution_store.create(
                    issue_number,
                    project_item_id=item.id,
                    branch=branch,
                    worktree_path=str(worktree_path),
                    base_ref=selected_base_ref,
                    codex_model=self.config.providers.codex_model,
                    gemini_model=self.config.providers.gemini_model,
                    repository_identity=self.config.github.repository_full_name,
                    contract_fingerprint=(self.project_contract.fingerprint if self.project_contract else None),
                    project_contract_json=(self.project_contract.to_json() if self.project_contract else None),
                )
                self._execution_id = record.id
            except Exception as error:
                raise RunPipelineError(
                    f"Falha ao persistir estado antes de criar o worktree: {error}"
                ) from error
        try:
            worktree = self.worktree_creator.create_worktree(
                self.config.workspace.repository_path,
                branch,
                worktree_path,
                selected_base_ref,
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao criar branch e worktree: {error}"
            ) from error
        self._transition(
            ExecutionPhase.CODEX_RUNNING,
            "Worktree preparado; execução Codex será iniciada",
            current_head_sha=(
                self.git_publisher.current_head(worktree.path)
                if self.git_publisher and hasattr(self.git_publisher, "current_head")
                else None
            ),
        )
        try:
            self.status_writer.set_status(
                item.id, self.config.github.status_for("implementing")
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao alterar o Status para '{self.config.github.status_for('implementing')}'; "
                "worktree e branch foram preservados em "
                f"{worktree.path}: {error}"
            ) from error
        self._checkpoint(
            "Project marcado como em andamento",
            project_status=self.config.github.status_for("implementing"),
        )
        self._checkpoint("Primeira chamada Codex iniciada", codex_start_attempted=True)
        self._prepare_graph(worktree.path)
        try:
            execution = self.codex_executor.execute(
                worktree.path,
                build_initial_prompt(
                    issue, use_code_review_graph=self._uses_graph()
                ),
            )
        except ProviderFailure as error:
            self._record_provider_wait(error, ExecutionPhase.WAITING_CODEX_QUOTA)
            raise RunPipelineError(
                "Falha do Codex observada; execução preservada para retomada"
            ) from error
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao executar o Codex; o Status está em '{self.config.github.status_for('implementing')}' "
                "e o worktree foi preservado em "
                f"{worktree.path}: {error}"
            ) from error
        self._checkpoint("Sessão Codex iniciada", codex_session_id=execution.session_id)
        if (
            self.local_validator is None
            or self.git_publisher is None
            or self.pull_request_creator is None
        ):
            return RunResult(
                issue.number,
                item.id,
                worktree.branch,
                worktree.path,
                worktree.base_ref,
                execution.session_id,
                execution.final_message,
                self.config.github.status_for("implementing"),
            )
        self._transition(ExecutionPhase.TESTING, "Gates locais serão executados")
        try:
            gates, final_message = self._validate_with_recovery(
                issue, worktree, execution.session_id, execution.final_message
            )
        except ProviderFailure as error:
            self._record_provider_wait(error, ExecutionPhase.WAITING_PROVIDER)
            raise RunPipelineError("Falha de processo local; checkpoint preservado") from error
        except Exception as error:
            raise RunPipelineError(
                f"Falha nos gates locais; o Status está em '{self.config.github.status_for('implementing')}' "
                f"e o worktree foi preservado em {worktree.path}: {error}"
            ) from error
        self._transition(ExecutionPhase.COMMIT_PENDING, "Commit será publicado")
        try:
            commit_sha = self.git_publisher.commit(worktree.path, issue.number)
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao preparar ou criar o commit; worktree e staging foram preservados em "
                f"{worktree.path}: {error}"
            ) from error
        self._transition(ExecutionPhase.PUSH_PENDING, "Commit confirmado; push será publicado", current_head_sha=commit_sha)
        try:
            self.git_publisher.push(
                worktree.path, self.config.workspace.remote_name, worktree.branch
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao enviar a branch; o commit {commit_sha} foi preservado em {worktree.path}: {error}"
            ) from error
        self._transition(ExecutionPhase.PR_PENDING, "Push confirmado; Pull Request será criado", current_head_sha=commit_sha)
        try:
            pull_request = self.pull_request_creator.create(
                issue, worktree.branch, gates
            )
            self._checkpoint(
                "Identidade do Pull Request criado preservada antes da convergência",
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
            )
            self._wait_for_pull_request_head(
                pull_request, worktree.branch, commit_sha, stale_head_sha=None
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao criar Pull Request; branch publicada e commit {commit_sha} foram preservados: {error}"
            ) from error
        self._transition(
            ExecutionPhase.WAITING_CI,
            "Pull Request publicado; aguardando CI",
            head_sha=commit_sha,
            current_head_sha=commit_sha,
            pull_request_number=pull_request.number,
            pull_request_url=pull_request.url,
        )
        try:
            self.status_writer.set_status(item.id, self.config.github.status_for("ai_review"))
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já criado em {pull_request.url}, mas falhou ao alterar "
                f"o Status para '{self.config.github.status_for('ai_review')}': {error}"
            ) from error
        self._checkpoint(
            "Project marcado para revisão IA",
            project_status=self.config.github.status_for("ai_review"),
        )
        if self.ci_reader is None:
            return RunResult(
                issue.number,
                item.id,
                worktree.branch,
                worktree.path,
                worktree.base_ref,
                execution.session_id,
            final_message,
                self.config.github.status_for("ai_review"),
                gates,
                commit_sha,
                self.config.workspace.remote_name,
                pull_request.number,
                pull_request.url,
                pull_request.base,
            )
        try:
            ci_result = CiGate(
                self.ci_reader,
                self.config.ci,
                discovered_checks=(self.project_contract.expected_ci if self.project_contract else ()),
            ).wait(
                pull_request.number, commit_sha
            )
        except CiFailureError:
            raise
        except Exception as error:
            raise RunPipelineError(
                f"Falha no gate de CI da Issue #{issue.number}, Pull Request #{pull_request.number} "
                f"em {pull_request.url}; Status permanece em '{self.config.github.status_for('ai_review')}', "
                f"branch {worktree.branch}, commit {commit_sha} e worktree {worktree.path} foram preservados: {error}"
            ) from error
        base_result = RunResult(
            issue.number,
            item.id,
            worktree.branch,
            worktree.path,
            worktree.base_ref,
            execution.session_id,
                final_message,
            self.config.github.status_for("ai_review"),
            gates,
            commit_sha,
            self.config.workspace.remote_name,
            pull_request.number,
            pull_request.url,
            pull_request.base,
            ci_result.expected_head_sha,
            ci_result.checks,
            ci_result.status,
        )
        if self.review_reader is None or self.reviewer is None:
            return base_result
        self._transition(
            ExecutionPhase.GEMINI_REVIEWING,
            "Revisão independente será executada",
            head_sha=ci_result.expected_head_sha,
            ci_head_sha=ci_result.expected_head_sha,
            ci_checks_json=self._ci_checks_json(ci_result.checks),
        )
        try:
            review, ci_result, gates, final_message, corrections, prior_findings = (
                self._run_review_loop(
                    issue,
                    worktree,
                    pull_request,
                    execution.session_id,
                    ci_result,
                    gates,
                    final_message,
                )
            )
        except ProviderFailure as error:
            waiting_phase = (
                ExecutionPhase.WAITING_CODEX_QUOTA
                if error.provider == "codex"
                else ExecutionPhase.WAITING_GEMINI_QUOTA
            )
            self._record_provider_wait(error, waiting_phase)
            raise RunPipelineError(
                f"Falha do provider {error.provider} observada; execução preservada para retomada"
            ) from error
        except Exception as error:
            raise RunPipelineError(
                f"Falha na revisão Gemini da Issue #{issue.number}, Pull Request #{pull_request.number} em {pull_request.url}; "
                f"Status permanece em '{self.config.github.status_for('ai_review')}', branch {worktree.branch}, worktree {worktree.path} "
                f"e sessão Codex {execution.session_id} foram preservados; nenhum merge foi executado: {error}"
            ) from error
        result = RunResult(
            **{
                **base_result.__dict__,
                "gates": gates,
                "final_message": final_message,
                "commit_sha": ci_result.expected_head_sha,
                "pull_request_head_sha": ci_result.expected_head_sha,
                "ci_checks": ci_result.checks,
                "ci_status": ci_result.status,
                "review": review,
                "blocking_severities": self.config.review.blocking_severities,
                "review_attempts": corrections + 1,
                "correction_attempts": corrections,
                "final_reviewed_head_sha": review.reviewed_head_sha,
                "prior_findings_count": len(prior_findings),
                "auto_merge_enabled": self.config.execution.auto_merge,
                "reviewed_head_sha": review.reviewed_head_sha,
            }
        )
        if not self.config.execution.auto_merge:
            self._transition(
                ExecutionPhase.APPROVED_AWAITING_ACTION,
                "Review aprovado; aguardando ação externa",
                head_sha=review.reviewed_head_sha,
                reviewed_head_sha=review.reviewed_head_sha,
                review_verdict=review.verdict.value,
            )
            return result
        return self._merge_approved_pull_request(
            result, worktree, pull_request, ci_result, review
        )

    def _merge_approved_pull_request(
        self,
        result: RunResult,
        worktree: GitWorktree,
        pull_request: PullRequest,
        ci_result,
        review: StructuredReview,
    ) -> RunResult:
        """Revalida tudo uma última vez e só então executa a única mutação de merge."""
        if self.pull_request_merger is None or self.git_publisher is None:
            raise RunPipelineError(
                "Auto-merge foi habilitado, mas a infraestrutura de merge não está disponível"
            )
        try:
            branch, local_head = self.git_publisher.merge_state(worktree.path)
            snapshot = self.pull_request_merger.get_merge_snapshot(pull_request.number)
            MergeGate().validate(
                snapshot,
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
                base=self.config.github.pull_request_base,
                branch=worktree.branch,
                local_head=local_head,
                review=review,
                ci_result=ci_result,
                blocking_severities=self.config.review.blocking_severities,
            )
            if branch != worktree.branch:
                raise MergeGateError("Branch local divergiu da branch da Issue")
            self._transition(
                ExecutionPhase.MERGE_PENDING,
                "Merge remoto será solicitado",
                head_sha=review.reviewed_head_sha,
                reviewed_head_sha=review.reviewed_head_sha,
                review_verdict=review.verdict.value,
            )
            merge = self.pull_request_merger.merge(
                pull_request.number, review.reviewed_head_sha
            )
            wait_for_merge_confirmation(
                self.convergence,
                lambda: self.pull_request_merger.get_merge_snapshot(
                    pull_request.number
                ),
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
                expected_head_sha=review.reviewed_head_sha,
                expected_merge_commit_sha=merge.merge_commit_sha,
            )
            self.pull_request_merger.verify_merge_commit(
                merge.merge_commit_sha, merge.merged_head_sha
            )
        except Exception as error:
            raise RunPipelineError(
                f"Auto-merge recusado ou não confirmado para Pull Request #{pull_request.number}; nenhum Status Done foi escrito: {error}",
                reason="MERGE_BLOCKED",
            ) from error
        try:
            self._transition(
                ExecutionPhase.PROJECT_DONE_PENDING,
                "Merge confirmado no GitHub; Project Done será atualizado",
                merge_commit_sha=merge.merge_commit_sha,
                merged_head_sha=merge.merged_head_sha,
                head_sha=merge.merged_head_sha,
            )
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já foi merged ({merge.merge_commit_sha}), mas falhou ao persistir confirmação para reconciliação: {error}"
            ) from error
        try:
            self.status_writer.set_status(
                result.project_item_id, self.config.github.status_for("completed")
            )
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já foi merged ({merge.merge_commit_sha}), mas falhou ao atualizar o Status para 'Done': {error}"
            ) from error
        try:
            self._transition(
                ExecutionPhase.COMPLETED,
                "Merge e Project Done confirmados",
                project_status=self.config.github.status_for("completed"),
                merge_commit_sha=merge.merge_commit_sha,
                merged_head_sha=merge.merged_head_sha,
            )
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já foi merged e o Project já está Done, "
                f"mas falhou ao registrar conclusão para reconciliação: {error}"
            ) from error
        return RunResult(
            **{
                **result.__dict__,
                "project_status": self.config.github.status_for("completed"),
                "merge_status": "SUCCESS",
                "merged": True,
                "merge_commit_sha": merge.merge_commit_sha,
                "merged_head_sha": merge.merged_head_sha,
            }
        )

    def _review_head(
        self,
        issue: Issue,
        worktree: GitWorktree,
        pull_request: PullRequest,
        head_sha: str,
        gates: tuple[GateResult, ...],
        ci_result,
        prior_findings: tuple[ReviewFinding, ...],
    ) -> StructuredReview:
        assert self.review_reader is not None and self.reviewer is not None
        if self._uses_graph():
            try:
                self.graph_integrator.ensure_antigravity_mcp()
            except Exception as error:
                logger.warning(
                    "MCP CRG do Antigravity indisponível; review seguirá pelo dossier: %s",
                    str(error)[:500],
                )
        self._prepare_graph(worktree.path)
        context_builder = ContextBuilder(self.review_reader, worktree.path)
        dossier = context_builder.build(
            issue, pull_request.number, head_sha, gates, ci_result, prior_findings
        )
        policy_path = (
            Path(__file__).parents[3] / "prompts" / "gemini" / "review_policy.md"
        )
        policy = policy_path.read_text(encoding="utf-8")
        plan = parse_review_plan(
            self.reviewer.invoke(
                build_prompt(
                    policy, dossier,
                    blocking_severities=self.config.review.blocking_severities,
                    use_code_review_graph=self._uses_graph(),
                    graph_repository=worktree.path,
                ),
                worktree.path, REVIEW_PLAN_SCHEMA
            )
        )
        context_builder.ensure_head_is_current(pull_request.number, head_sha)
        review = parse_structured_review(
            self.reviewer.invoke(
                build_prompt(
                    policy, dossier, plan, build_checklists(dossier.changed_files),
                    blocking_severities=self.config.review.blocking_severities,
                    use_code_review_graph=self._uses_graph(),
                    graph_repository=worktree.path,
                ),
                worktree.path,
                STRUCTURED_REVIEW_SCHEMA,
            ),
            head_sha,
            self.config.review.blocking_severities,
        )
        context_builder.ensure_head_is_current(pull_request.number, head_sha)
        return review

    def _run_review_loop(
        self,
        issue: Issue,
        worktree: GitWorktree,
        pull_request: PullRequest,
        session_id: str,
        ci_result,
        gates: tuple[GateResult, ...],
        final_message: str,
    ):
        """Executa revisões frescas e retoma exclusivamente a sessão inicial do Codex."""
        review = self._review_head(
            issue,
            worktree,
            pull_request,
            ci_result.expected_head_sha,
            gates,
            ci_result,
            (),
        )
        self._record_review(review)
        prior_findings: tuple[ReviewFinding, ...] = ()
        corrections = 0
        while review.verdict is ReviewVerdict.REJECTED:
            self._transition(
                ExecutionPhase.NEEDS_CHANGES,
                "Review rejeitado; correção necessária",
                head_sha=ci_result.expected_head_sha,
                reviewed_head_sha=review.reviewed_head_sha,
                review_verdict=review.verdict.value,
                correction_attempts=corrections,
            )
            previous_findings = prior_findings
            prior_findings += review.findings
            if corrections >= self.config.review.max_correction_attempts:
                raise RunPipelineError(
                    f"Limite de correções atingido para Issue #{issue.number}, Pull Request #{pull_request.number}, "
                    f"HEAD {ci_result.expected_head_sha}, tentativa {corrections}, sessão {session_id} preservada; nenhum merge foi executado"
                )
            corrections += 1
            self._transition(
                ExecutionPhase.CODEX_RUNNING,
                "Sessão Codex será retomada para correção",
                correction_attempts=corrections,
            )
            prompt = CorrectionContextBuilder().build(
                issue,
                pull_request.number,
                pull_request.url,
                ci_result.expected_head_sha,
                review,
                previous_findings,
                use_code_review_graph=self._uses_graph(),
            )
            self._prepare_graph(worktree.path)
            execution = self.codex_executor.resume(worktree.path, session_id, prompt)
            if execution.session_id != session_id:
                raise RunPipelineError(
                    "Codex retomou uma sessão diferente da sessão original"
                )
            final_message = execution.final_message
            assert (
                self.local_validator is not None
                and self.git_publisher is not None
                and self.ci_reader is not None
            )
            self._transition(
                ExecutionPhase.TESTING, "Gates locais da correção serão executados",
                provider_retry_attempts=0
            )
            gates, final_message = self._validate_with_recovery(
                issue, worktree, session_id, final_message
            )
            self._ensure_existing_pull_request(
                pull_request, worktree.branch, ci_result.expected_head_sha
            )
            self._ensure_local_head_is_current(
                worktree.path, ci_result.expected_head_sha
            )
            self._transition(ExecutionPhase.COMMIT_PENDING, "Commit da correção será publicado")
            new_head = self.git_publisher.commit_correction(worktree.path)
            self._checkpoint("HEAD da correção preservado", current_head_sha=new_head)
            self._ensure_existing_pull_request(
                pull_request, worktree.branch, ci_result.expected_head_sha
            )
            self._transition(
                ExecutionPhase.PUSH_PENDING, "Commit da correção confirmado; push será publicado",
                current_head_sha=new_head, head_sha=new_head, ci_head_sha=None,
                reviewed_head_sha=None, review_verdict=None,
                merge_commit_sha=None, merged_head_sha=None,
            )
            self.git_publisher.push(
                worktree.path, self.config.workspace.remote_name, worktree.branch
            )
            self._transition(
                ExecutionPhase.PR_PENDING,
                "Push da correção confirmado; Pull Request existente será revalidado",
                current_head_sha=new_head,
            )
            self._wait_for_pull_request_head(
                pull_request,
                worktree.branch,
                new_head,
                stale_head_sha=ci_result.expected_head_sha,
            )
            self._transition(
                ExecutionPhase.WAITING_CI,
                "Correção publicada; aguardando CI",
                head_sha=new_head,
                current_head_sha=new_head,
                correction_attempts=corrections,
            )
            ci_result = CiGate(
                self.ci_reader,
                self.config.ci,
                discovered_checks=(self.project_contract.expected_ci if self.project_contract else ()),
            ).wait(
                pull_request.number,
                new_head,
                stale_head_sha=review.reviewed_head_sha,
            )
            self._transition(
                ExecutionPhase.GEMINI_REVIEWING,
                "Nova revisão independente será executada",
                head_sha=new_head,
                ci_head_sha=ci_result.expected_head_sha,
                ci_checks_json=self._ci_checks_json(ci_result.checks),
            )
            review = self._review_head(
                issue,
                worktree,
                pull_request,
                new_head,
                gates,
                ci_result,
                prior_findings,
            )
            self._record_review(review)
        return review, ci_result, gates, final_message, corrections, prior_findings

    def _validate_with_recovery(
        self,
        issue: Issue,
        worktree: GitWorktree,
        session_id: str,
        final_message: str,
    ) -> tuple[tuple[GateResult, ...], str]:
        """Corrige falha determinística no mesmo run, sessão, worktree e branch."""
        assert self.local_validator is not None
        self._ensure_contract_is_frozen(worktree)
        attempts = 0
        if self.execution_store is not None and self._execution_id is not None:
            attempts = self.execution_store.get(self._execution_id).local_gate_correction_attempts
        while True:
            try:
                if self.project_contract is None:
                    gates = self.local_validator.validate(worktree.path)
                else:
                    gates = self.local_validator.validate(worktree.path, self.project_contract)
                self._record_gate_results(gates, attempts)
                return gates, final_message
            except LocalValidationError as error:
                if isinstance(error, ProviderFailure):
                    raise
                if error.result is not None:
                    self._record_gate_results((error.result,), attempts)
                limit = self.config.execution.max_local_gate_correction_attempts
                if attempts >= limit:
                    message = (
                        f"Limite de correções de gates locais atingido ({attempts}/{limit}); "
                        f"execução {self._execution_id or '-'} e sessão {session_id} preservadas"
                    )
                    if self.execution_store is not None and self._execution_id is not None:
                        self.execution_store.require_human(
                            self._execution_id, summary=message, reason="LOCAL_GATE_CORRECTION_LIMIT"
                        )
                    raise RunPipelineError(message, reason="LOCAL_GATE_CORRECTION_LIMIT") from error
                attempts += 1
                self._transition(
                    ExecutionPhase.CODEX_RUNNING,
                    "Gate local falhou; mesma sessão Codex será retomada",
                    local_gate_correction_attempts=attempts,
                )
                diagnostic = str(error)[:500]
                self._prepare_graph(worktree.path)
                resumed = self.codex_executor.resume(
                    worktree.path,
                    session_id,
                    "Corrija somente a falha determinística dos gates locais abaixo. "
                    "Mantenha o escopo da Issue e não faça commit, push ou PR.\n\n"
                    + diagnostic
                    + (
                        f"\n\n{GRAPH_INSTRUCTION}"
                        if self._uses_graph() else ""
                    ),
                )
                if resumed.session_id != session_id:
                    raise RunPipelineError("Codex retomou uma sessão diferente da sessão original")
                final_message = resumed.final_message
                self._transition(
                    ExecutionPhase.TESTING,
                    "Correção local concluída; o mesmo plano congelado será reexecutado",
                    local_gate_correction_attempts=attempts,
                )

    def _prepare_graph(self, worktree: Path) -> None:
        """Executa a integração opcional sem permitir propagação de falhas."""
        if not self._uses_graph():
            return
        try:
            self.graph_integrator.prepare(worktree)
        except Exception as error:
            logger.warning(
                "Code Review Graph indisponível; usando busca/leitura direta: %s",
                str(error)[:500],
            )

    def _uses_graph(self) -> bool:
        graph_config = getattr(self.config, "code_review_graph", None)
        return bool(graph_config and graph_config.enabled)

    def _record_gate_results(self, gates: tuple[GateResult, ...], attempt: int) -> None:
        if self.execution_store is None or self._execution_id is None:
            return
        record = self.execution_store.get(self._execution_id)
        history: list[dict[str, object]] = []
        if record.gate_results_json:
            try:
                parsed = json.loads(record.gate_results_json)
                if isinstance(parsed, list):
                    history = [item for item in parsed if isinstance(item, dict)]
            except json.JSONDecodeError:
                history = []
        history.extend(
            {
                "name": gate.name,
                "category": gate.category,
                "succeeded": gate.succeeded,
                "returncode": gate.returncode,
                "duration_seconds": round(gate.duration_seconds, 6),
                "attempt": attempt,
            }
            for gate in gates
        )
        self._checkpoint(
            "Resultado de gate local persistido",
            local_gate_correction_attempts=attempt,
            gate_results_json=json.dumps(history[-100:], ensure_ascii=False, separators=(",", ":")),
        )

    @staticmethod
    def _ci_checks_json(checks: tuple[StatusCheck, ...]) -> str:
        return json.dumps(
            [
                {"name": check.name, "status": check.status, "conclusion": check.conclusion}
                for check in checks
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _ensure_contract_is_frozen(self, worktree: GitWorktree) -> None:
        """Mudança do contrato durante a Issue exige decisão auditável, nunca silêncio."""
        if self.project_contract is None:
            return
        uses_override = any(
            evidence.kind == "explicit_override"
            for plan in (*self.project_contract.bootstrap, *self.project_contract.gates)
            for evidence in plan.source_evidence
        )
        current = ProjectCapabilityResolver().resolve(
            worktree.path,
            repository_identity=self.project_contract.repository_identity,
            base_branch=self.project_contract.base_branch,
            pull_request_target=self.project_contract.pull_request_target,
            protected_branches=self.project_contract.protected_branches,
            overrides=(
                (*self.project_contract.bootstrap, *self.project_contract.gates)
                if uses_override
                else ()
            ),
        )
        if current.fingerprint == self.project_contract.fingerprint:
            return
        message = (
            "Arquivos que definem build/testes/CI alteraram o contrato durante o run; "
            f"congelado={self.project_contract.fingerprint}, observado={current.fingerprint}"
        )
        if self.execution_store is not None and self._execution_id is not None:
            self.execution_store.require_human(
                self._execution_id, summary=message, reason="PROJECT_CONTRACT_CHANGED"
            )
        raise RunPipelineError(message, reason="PROJECT_CONTRACT_CHANGED")

    def _record_review(self, review: StructuredReview) -> None:
        """Persiste o veredito antes de qualquer transição dependente dele."""
        if self.execution_store is None or self._execution_id is None:
            return
        recorder = getattr(self.execution_store, "record_review", None)
        if recorder is None:
            raise RunPipelineError("Store não suporta persistência estruturada de review")
        recorder(self._execution_id, review, "Review independente persistida")

    def _ensure_existing_pull_request(
        self, pull_request: PullRequest, branch: str, expected_head_sha: str
    ) -> None:
        """Recusa publicar correções em PR trocado, fechado ou com HEAD divergente."""
        assert self.review_reader is not None
        data = self.review_reader.get_review_data(pull_request.number)
        if (
            not isinstance(data, dict)
            or data.get("number") != pull_request.number
            or data.get("headRefName") != branch
            or data.get("headRefOid") != expected_head_sha
            or data.get("url") != pull_request.url
            or data.get("state") != "OPEN"
        ):
            raise RunPipelineError(
                "O Pull Request existente divergiu, foi fechado ou não aponta para o novo HEAD",
                reason="REMOTE_AMBIGUOUS",
            )

    def _wait_for_pull_request_head(
        self,
        pull_request: PullRequest,
        branch: str,
        expected_head_sha: str,
        *,
        stale_head_sha: str | None,
    ) -> None:
        """Aguarda apenas o HEAD anterior conhecido; demais divergências falham."""
        if self.review_reader is None and self.pull_request_merger is None:
            return

        def classify(data) -> ObservationDecision:
            if isinstance(data, MergePullRequestSnapshot):
                if (
                    data.number != pull_request.number
                    or data.url != pull_request.url
                    or data.head_branch != branch
                    or data.state != "OPEN"
                ):
                    raise RunPipelineError(
                        "O Pull Request existente divergiu, foi fechado ou trocou de identidade"
                    )
                observed_head = data.head_sha
            else:
                observed_head = data.get("headRefOid") if isinstance(data, dict) else None
            if (
                not isinstance(data, (dict, MergePullRequestSnapshot))
                or (
                    isinstance(data, dict)
                    and (
                        data.get("number") != pull_request.number
                        or data.get("headRefName") != branch
                        or data.get("state") not in {None, "OPEN"}
                    )
                )
            ):
                raise RunPipelineError(
                    "O Pull Request existente divergiu, foi fechado ou trocou de identidade"
                )
            if observed_head == expected_head_sha:
                return ObservationDecision.CONVERGED
            if stale_head_sha is not None and observed_head == stale_head_sha:
                return ObservationDecision.RETRY
            raise RunPipelineError(
                "O Pull Request existente aponta para um HEAD inesperado"
            )

        def read():
            if self.pull_request_merger is not None:
                return self.pull_request_merger.get_merge_snapshot(pull_request.number)
            assert self.review_reader is not None
            return self.review_reader.get_review_data(pull_request.number)

        self.convergence.wait(
            read,
            classify,
            f"HEAD {expected_head_sha} do Pull Request #{pull_request.number}",
        )

    def _ensure_local_head_is_current(
        self, worktree: Path, expected_head_sha: str
    ) -> None:
        """Impede publicar um commit que o Codex tenha criado fora do control plane."""
        assert self.git_publisher is not None
        observed_head = self.git_publisher.current_head(worktree)
        if observed_head != expected_head_sha:
            raise RunPipelineError(
                "O HEAD local divergiu do HEAD revisado; a publicação da correção foi recusada",
                reason="REMOTE_AMBIGUOUS",
            )

    def _find_project_item(self, issue_number: int) -> ProjectItem:
        repository = self.config.github.repository_full_name
        matches = [
            item
            for item in self.project_reader.list_items()
            if item.is_issue
            and item.repository == repository
            and item.issue_number == issue_number
        ]
        if not matches:
            raise RunPipelineError(
                f"A etapa de localizar item do Project falhou: Issue #{issue_number} não encontrada"
            )
        if len(matches) > 1:
            raise RunPipelineError(
                f"A etapa de localizar item do Project falhou: Issue #{issue_number} é ambígua"
            )
        return matches[0]

    def _transition(
        self, phase: ExecutionPhase, summary: str, **updates: object
    ) -> None:
        if self.execution_store is None or self._execution_id is None:
            return
        try:
            run = self.execution_store.transition(
                self._execution_id, phase, summary=summary, **updates
            )
            # Entregas externas são auxiliares e o serviço absorve suas falhas.
            from ai_dev_orchestrator.services.escalation import EscalationService
            try:
                EscalationService(self.config, self.execution_store).deliver_event(run)
            except Exception:
                logger.warning("Notificação externa indisponível; pipeline continuará")
        except Exception as error:
            raise RunPipelineError(f"Falha ao persistir checkpoint: {error}") from error

    def _checkpoint(self, summary: str, **updates: object) -> None:
        if self.execution_store is None or self._execution_id is None:
            return
        try:
            self.execution_store.checkpoint(
                self._execution_id, summary=summary, **updates
            )
        except Exception as error:
            raise RunPipelineError(f"Falha ao persistir checkpoint: {error}") from error

    def _record_provider_wait(
        self, failure: ProviderFailure, phase: ExecutionPhase
    ) -> None:
        """Aplica a mesma política usada pela retomada e pelo supervisor."""
        from ai_dev_orchestrator.services.provider_recovery import record_provider_failure
        if self.execution_store is not None and self._execution_id is not None:
            run = record_provider_failure(self.execution_store, self._execution_id, failure)
            if run.phase == ExecutionPhase.BLOCKED_PROVIDER:
                raise RunPipelineError(run.last_error) from failure
