"""Coordena a execução de uma Issue sem ocultar mutações."""

from __future__ import annotations

from dataclasses import dataclass
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
from ai_dev_orchestrator.services.ci_gate import CiGate, PullRequestCiReader
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
)
from ai_dev_orchestrator.domain.execution import ExecutionPhase, ExecutionStore
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore


class RunPipelineError(Exception):
    """Indica em qual etapa a execução foi interrompida."""


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


def build_initial_prompt(issue: Issue) -> str:
    """Monta o prompt inicial de forma determinística, sem consultar providers."""
    return (
        f"Implemente a Issue #{issue.number}: {issue.title}\n\n"
        "Body completo da Issue:\n"
        f"{issue.body}\n\n"
        "Você está executando dentro do worktree já preparado para esta Issue. "
        "Leia e respeite o AGENTS.md do repositório/worktree. Trabalhe somente no escopo "
        "desta Issue. Nesta etapa, não faça commit, push, Pull Request ou merge. "
        "Execute as validações pedidas pela própria Issue quando aplicável."
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
        self._execution_id: str | None = None

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> RunPipeline:
        pull_requests = GitHubPullRequestAdapter(config)
        return cls(
            config,
            GitHubIssueAdapter(config),
            GitHubProjectAdapter(config),
            GitHubProjectStatusAdapter(config),
            GitWorktreeAdapter(),
            CodexAdapter(),
            LocalValidationService(),
            GitPublicationAdapter(),
            pull_requests,
            GitHubCiAdapter(config),
            pull_requests,
            AntigravityAdapter(config.review.timeout_seconds),
            pull_requests,
            SqliteExecutionStore(config.state.database_path),
        )

    def run(self, issue_number: int, branch: str) -> RunResult:
        if issue_number <= 0:
            raise RunPipelineError("A Issue deve ser um inteiro positivo")
        try:
            issue = self.issue_reader.get_issue(issue_number)
            item = self._find_project_item(issue_number)
            if not is_eligible_for_execution(
                item,
                self.config.github.repository_full_name,
                self.config.github.ready_status,
            ):
                raise RunPipelineError(
                    f"A etapa de validar elegibilidade falhou: a Issue #{issue_number} não está em "
                    f"'{self.config.github.ready_status}' no repositório configurado"
                )
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
                    base_ref=self.config.workspace.base_ref,
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
                self.config.workspace.base_ref,
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
                item.id, self.config.github.in_progress_status
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao alterar o Status para '{self.config.github.in_progress_status}'; "
                "worktree e branch foram preservados em "
                f"{worktree.path}: {error}"
            ) from error
        self._checkpoint(
            "Project marcado como em andamento",
            project_status=self.config.github.in_progress_status,
        )
        try:
            execution = self.codex_executor.execute(
                worktree.path, build_initial_prompt(issue)
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha ao executar o Codex; o Status está em '{self.config.github.in_progress_status}' "
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
                self.config.github.in_progress_status,
            )
        self._transition(ExecutionPhase.TESTING, "Gates locais serão executados")
        try:
            gates = self.local_validator.validate(worktree.path)
        except Exception as error:
            raise RunPipelineError(
                f"Falha nos gates locais; o Status está em '{self.config.github.in_progress_status}' "
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
            self.status_writer.set_status(item.id, self.config.github.ai_review_status)
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já criado em {pull_request.url}, mas falhou ao alterar "
                f"o Status para '{self.config.github.ai_review_status}': {error}"
            ) from error
        self._checkpoint(
            "Project marcado para revisão IA",
            project_status=self.config.github.ai_review_status,
        )
        if self.ci_reader is None:
            return RunResult(
                issue.number,
                item.id,
                worktree.branch,
                worktree.path,
                worktree.base_ref,
                execution.session_id,
                execution.final_message,
                self.config.github.ai_review_status,
                gates,
                commit_sha,
                self.config.workspace.remote_name,
                pull_request.number,
                pull_request.url,
                pull_request.base,
            )
        try:
            ci_result = CiGate(self.ci_reader, self.config.ci).wait(
                pull_request.number, commit_sha
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha no gate de CI da Issue #{issue.number}, Pull Request #{pull_request.number} "
                f"em {pull_request.url}; Status permanece em '{self.config.github.ai_review_status}', "
                f"branch {worktree.branch}, commit {commit_sha} e worktree {worktree.path} foram preservados: {error}"
            ) from error
        base_result = RunResult(
            issue.number,
            item.id,
            worktree.branch,
            worktree.path,
            worktree.base_ref,
            execution.session_id,
            execution.final_message,
            self.config.github.ai_review_status,
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
                    execution.final_message,
                )
            )
        except Exception as error:
            raise RunPipelineError(
                f"Falha na revisão Gemini da Issue #{issue.number}, Pull Request #{pull_request.number} em {pull_request.url}; "
                f"Status permanece em '{self.config.github.ai_review_status}', branch {worktree.branch}, worktree {worktree.path} "
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
            confirmed = self.pull_request_merger.get_merge_snapshot(pull_request.number)
            if (
                confirmed.state != "MERGED"
                or not confirmed.merged
                or confirmed.merge_commit_sha != merge.merge_commit_sha
                or confirmed.head_sha != review.reviewed_head_sha
            ):
                raise MergeGateError("GitHub não confirmou o merge do HEAD aprovado")
            self.pull_request_merger.verify_merge_commit(
                merge.merge_commit_sha, merge.merged_head_sha
            )
        except Exception as error:
            raise RunPipelineError(
                f"Auto-merge recusado ou não confirmado para Pull Request #{pull_request.number}; nenhum Status Done foi escrito: {error}"
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
            self.status_writer.set_status(result.project_item_id, "Done")
        except Exception as error:
            raise RunPipelineError(
                f"Pull Request #{pull_request.number} já foi merged ({merge.merge_commit_sha}), mas falhou ao atualizar o Status para 'Done': {error}"
            ) from error
        try:
            self._transition(
                ExecutionPhase.COMPLETED,
                "Merge e Project Done confirmados",
                project_status="Done",
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
                "project_status": "Done",
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
                build_prompt(policy, dossier), worktree.path, REVIEW_PLAN_SCHEMA
            )
        )
        context_builder.ensure_head_is_current(pull_request.number, head_sha)
        review = parse_structured_review(
            self.reviewer.invoke(
                build_prompt(
                    policy, dossier, plan, build_checklists(dossier.changed_files)
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
            )
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
                ExecutionPhase.TESTING, "Gates locais da correção serão executados"
            )
            gates = self.local_validator.validate(worktree.path)
            self._ensure_existing_pull_request(
                pull_request, worktree.branch, ci_result.expected_head_sha
            )
            self._ensure_local_head_is_current(
                worktree.path, ci_result.expected_head_sha
            )
            self._transition(ExecutionPhase.COMMIT_PENDING, "Commit da correção será publicado")
            new_head = self.git_publisher.commit_correction(worktree.path)
            self._ensure_existing_pull_request(
                pull_request, worktree.branch, ci_result.expected_head_sha
            )
            self._transition(ExecutionPhase.PUSH_PENDING, "Commit da correção confirmado; push será publicado", current_head_sha=new_head)
            self.git_publisher.push(
                worktree.path, self.config.workspace.remote_name, worktree.branch
            )
            self._ensure_existing_pull_request(pull_request, worktree.branch, new_head)
            self._transition(
                ExecutionPhase.WAITING_CI,
                "Correção publicada; aguardando CI",
                head_sha=new_head,
                current_head_sha=new_head,
                correction_attempts=corrections,
            )
            ci_result = CiGate(self.ci_reader, self.config.ci).wait(
                pull_request.number, new_head
            )
            self._transition(
                ExecutionPhase.GEMINI_REVIEWING,
                "Nova revisão independente será executada",
                head_sha=new_head,
                ci_head_sha=ci_result.expected_head_sha,
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
        return review, ci_result, gates, final_message, corrections, prior_findings

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
                "O Pull Request existente divergiu, foi fechado ou não aponta para o novo HEAD"
            )

    def _ensure_local_head_is_current(
        self, worktree: Path, expected_head_sha: str
    ) -> None:
        """Impede publicar um commit que o Codex tenha criado fora do control plane."""
        assert self.git_publisher is not None
        observed_head = self.git_publisher.current_head(worktree)
        if observed_head != expected_head_sha:
            raise RunPipelineError(
                "O HEAD local divergiu do HEAD revisado; a publicação da correção foi recusada"
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
            self.execution_store.transition(
                self._execution_id, phase, summary=summary, **updates
            )
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
