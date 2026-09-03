"""Retomada fail-closed de execuções persistidas.

O serviço deliberadamente reconcilia apenas fatos que pode provar.  Quando um
checkpoint não permite decidir se uma mutação aconteceu, ele consulta a fonte
externa ou interrompe, nunca tenta a mutação novamente por tentativa e erro.
"""

from __future__ import annotations

from pathlib import Path
import time

from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord, TERMINAL_PHASES
from ai_dev_orchestrator.domain.review import ReviewVerdict
from ai_dev_orchestrator.domain.review import StructuredReview
from ai_dev_orchestrator.domain.worktree import GitWorktree
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.ci_gate import CiGate
from ai_dev_orchestrator.services.merge import MergeGate
from ai_dev_orchestrator.services.pipeline import RunPipeline, build_initial_prompt
from ai_dev_orchestrator.services.review import CorrectionContextBuilder


class RecoveryError(Exception):
    """A execução exige reconciliação manual ou não pode ser retomada."""


class ResumeService:
    """Reconstrói uma execução no mesmo `execution_id`, sem criar um novo run."""

    def __init__(self, config: OrchestratorConfig, pipeline: RunPipeline, store: SqliteExecutionStore) -> None:
        self.config, self.pipeline, self.store = config, pipeline, store
        self._pending_rejected_review: StructuredReview | None = None

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "ResumeService":
        return cls(config, RunPipeline.from_config(config), SqliteExecutionStore(config.state.database_path))

    def resume(self, issue_number: int) -> RunRecord:
        record = self.store.get_active_for_issue(issue_number)
        if record is None:
            latest = self.store.get_latest_for_issue(issue_number)
            if latest is not None and latest.phase in TERMINAL_PHASES:
                raise RecoveryError(f"A execução da Issue #{issue_number} é terminal e não pode ser retomada")
            raise RecoveryError(f"Não há execução ativa para a Issue #{issue_number}")
        if record.phase in TERMINAL_PHASES:
            raise RecoveryError(f"A execução da Issue #{issue_number} é terminal e não pode ser retomada")

        self.pipeline._execution_id = record.id
        worktree = self._validate_worktree(record)
        pull_request = self._validate_pull_request(record)
        self._checkpoint("Retomada iniciada após revalidação", record.current_head_sha)
        record = self.store.get(record.id)

        if record.phase is ExecutionPhase.PREPARING:
            if worktree is None:
                raise RecoveryError("Worktree persistido ausente; a retomada não cria outro worktree")
            # Um worktree existente é reconhecido acima e nunca recriado.
            if record.project_item_id and record.project_status != self.config.github.in_progress_status:
                self.pipeline.status_writer.set_status(record.project_item_id, self.config.github.in_progress_status)
                self._checkpoint("Project marcado como em andamento na retomada", project_status=self.config.github.in_progress_status)
            self._transition(ExecutionPhase.CODEX_RUNNING, "Worktree persistido reconhecido")
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.CODEX_RUNNING:
            self._resume_codex(record, worktree)
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.TESTING:
            if worktree is None or self.pipeline.local_validator is None:
                raise RecoveryError("Não há infraestrutura para reexecutar gates locais")
            self.pipeline.local_validator.validate(worktree.path)
            advanced = self._transition(ExecutionPhase.PUBLISHING, "Gates locais reexecutados na retomada")
            if self.pipeline.git_publisher is None:
                return advanced
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.PUBLISHING:
            self._reconcile_publication(record, worktree, pull_request)
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.WAITING_CI:
            if pull_request is None or not record.current_head_sha or self.pipeline.ci_reader is None:
                raise RecoveryError("PR, HEAD ou leitor de CI ausente para retomada")
            ci = CiGate(self.pipeline.ci_reader, self.config.ci).wait(pull_request.number, record.current_head_sha)
            self._transition(ExecutionPhase.GEMINI_REVIEWING, "CI revalidada para o HEAD persistido", ci_head_sha=ci.expected_head_sha, head_sha=ci.expected_head_sha)
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.GEMINI_REVIEWING:
            self._refresh_review(record, worktree, pull_request)
            return self.store.get(record.id) if self.store.get(record.id).phase in TERMINAL_PHASES else self.resume(issue_number)
        if record.phase is ExecutionPhase.NEEDS_CHANGES:
            self._resume_correction(record, worktree, pull_request)
            return self.resume(issue_number)
        if record.phase is ExecutionPhase.MERGING:
            self._reconcile_merge(record, worktree, pull_request)
            return self.store.get(record.id) if self.store.get(record.id).phase in TERMINAL_PHASES else self.resume(issue_number)
        raise RecoveryError(f"Fase de retomada não suportada: {record.phase}")

    def _validate_worktree(self, record: RunRecord) -> GitWorktree | None:
        if not record.worktree_path or not record.branch or not record.base_ref:
            return None
        creator = self.pipeline.worktree_creator
        validator = getattr(creator, "validate_existing_worktree", None)
        if validator is None:
            # Fakes legítimos de testes podem não expor Git; em produção o adapter expõe.
            if not Path(record.worktree_path).is_dir():
                raise RecoveryError("O worktree persistido não existe")
            return GitWorktree(self.config.workspace.repository_path, Path(record.worktree_path), record.branch, record.base_ref)
        try:
            allow_dirty = record.phase in {
                ExecutionPhase.PREPARING, ExecutionPhase.CODEX_RUNNING,
                ExecutionPhase.TESTING, ExecutionPhase.NEEDS_CHANGES,
            } or record.phase is ExecutionPhase.PUBLISHING
            worktree = validator(self.config.workspace.repository_path, record.worktree_path, record.branch, record.base_ref, allow_dirty=allow_dirty)
            if (
                record.current_head_sha
                and record.phase is not ExecutionPhase.PUBLISHING
                and self.pipeline.git_publisher is not None
            ):
                observed = self.pipeline.git_publisher.current_head(worktree.path)
                if observed != record.current_head_sha:
                    raise RecoveryError("HEAD local divergiu do checkpoint persistido")
            return worktree
        except RecoveryError:
            raise
        except Exception as error:
            raise RecoveryError(f"Worktree persistido não é seguro para retomar: {error}") from error

    def _validate_pull_request(self, record: RunRecord) -> PullRequest | None:
        if record.pull_request_number is None:
            return None
        reader = self.pipeline.review_reader
        if reader is None or not record.branch or not record.pull_request_url:
            raise RecoveryError("Dados do Pull Request persistido estão incompletos")
        try:
            data = reader.get_review_data(record.pull_request_number)
        except Exception as error:
            raise RecoveryError(f"Não foi possível revalidar o Pull Request: {error}") from error
        expected = record.current_head_sha
        if (not isinstance(data, dict) or data.get("number") != record.pull_request_number
                or data.get("url") != record.pull_request_url
                or data.get("headRefName") != record.branch
                or data.get("baseRefName") != self.config.github.pull_request_base
                or (
                    expected is not None
                    and record.phase is not ExecutionPhase.PUBLISHING
                    and data.get("headRefOid") != expected
                )):
            raise RecoveryError("Pull Request persistido divergiu do repositório ou do HEAD esperado")
        return PullRequest(
            record.pull_request_number, record.pull_request_url, "", data["baseRefName"],
            record.branch, data.get("headRefOid", ""),
        )

    def _resume_codex(self, record: RunRecord, worktree: GitWorktree | None) -> RunRecord:
        if worktree is None:
            raise RecoveryError("Worktree persistido ausente; não é seguro iniciar sessão Codex")
        issue = self.pipeline.issue_reader.get_issue(record.issue_number)
        if record.codex_session_id:
            execution = self.pipeline.codex_executor.resume(worktree.path, record.codex_session_id, build_initial_prompt(issue))
        else:
            raise RecoveryError("Sessão Codex persistida ausente; a retomada não cria sessão nova")
        if record.codex_session_id and execution.session_id != record.codex_session_id:
            raise RecoveryError("Codex retornou uma sessão diferente da sessão persistida")
        return self._transition(ExecutionPhase.TESTING, "Sessão Codex persistida retomada")

    def _reconcile_publication(self, record: RunRecord, worktree: GitWorktree | None, pull_request: PullRequest | None) -> RunRecord:
        if worktree is None or not record.branch:
            raise RecoveryError("Dados locais insuficientes para reconciliar publicação")
        publisher = self.pipeline.git_publisher
        if publisher is None:
            raise RecoveryError("Publicador Git indisponível para reconciliação")
        previous_head = record.current_head_sha
        try:
            _, local_head = publisher.merge_state(worktree.path)
        except Exception:
            local_head = None
        if local_head is None:
            try:
                if record.correction_attempts:
                    local_head = publisher.commit_correction(worktree.path)
                else:
                    local_head = publisher.commit(worktree.path, record.issue_number)
            except Exception as error:
                raise RecoveryError(f"Não foi possível criar o commit local da retomada: {error}") from error
            record = self._checkpoint("Commit local reconciliado", local_head, current_head_sha=local_head)
        elif previous_head and local_head != previous_head:
            ancestor_reader = getattr(publisher, "is_ancestor", None)
            if ancestor_reader is None or not ancestor_reader(worktree.path, previous_head, local_head):
                raise RecoveryError("HEAD local divergiu do checkpoint sem relação de ancestralidade")
        remote_head_reader = getattr(publisher, "remote_head", None)
        if remote_head_reader is None:
            raise RecoveryError("Não é possível confirmar a branch remota sem leitura explícita")
        remote_head = remote_head_reader(worktree.path, self.config.workspace.remote_name, record.branch)
        if remote_head not in {None, previous_head, local_head}:
            raise RecoveryError("Branch remota aponta para SHA incompatível")
        if pull_request is not None and pull_request.head_sha not in {previous_head, local_head}:
            raise RecoveryError("Pull Request aponta para SHA incompatível")
        if remote_head is None or (
            remote_head == previous_head and remote_head != local_head
        ):
            try:
                publisher.push(worktree.path, self.config.workspace.remote_name, record.branch)
                remote_head = remote_head_reader(worktree.path, self.config.workspace.remote_name, record.branch)
            except Exception as error:
                raise RecoveryError(f"Não foi possível confirmar o push da branch: {error}") from error
        if remote_head != local_head:
            raise RecoveryError("Branch remota divergiu do HEAD local; push foi recusado")
        if pull_request is not None:
            pull_request = self._wait_for_pull_request_head(
                pull_request, local_head, previous_head
            )
        if pull_request is None:
            finder = getattr(self.pipeline.pull_request_creator, "find_open_by_branch", None)
            if finder is None:
                raise RecoveryError("Não é possível procurar PR existente com segurança")
            matches = finder(record.branch, self.config.github.pull_request_base)
            if len(matches) > 1:
                raise RecoveryError("Pull Requests existentes são ambíguos")
            if not matches:
                issue = self.pipeline.issue_reader.get_issue(record.issue_number)
                if self.pipeline.local_validator is None:
                    raise RecoveryError("Gates locais ausentes para criar o Pull Request")
                gates = self.pipeline.local_validator.validate(worktree.path)
                try:
                    pull_request = self.pipeline.pull_request_creator.create(issue, record.branch, gates)
                except Exception as error:
                    raise RecoveryError(f"Não foi possível criar Pull Request após reconciliação: {error}") from error
            else:
                pull_request = matches[0]
                if pull_request.head_sha != local_head:
                    raise RecoveryError("Pull Request encontrado aponta para HEAD divergente")
            record = self._checkpoint("PR existente reconciliado", local_head, pull_request_number=pull_request.number, pull_request_url=pull_request.url, current_head_sha=local_head)
        return self._transition(ExecutionPhase.WAITING_CI, "Publicação reconciliada sem novo push ou PR", head_sha=local_head, current_head_sha=local_head)

    def _wait_for_pull_request_head(
        self, pull_request: PullRequest, expected_head: str, previous_head: str | None
    ) -> PullRequest:
        """Reconsulta o PR por poucas tentativas; não executa mutações."""
        if self.pipeline.review_reader is None:
            raise RecoveryError("Leitor de Pull Request indisponível")
        for attempt in range(3):
            data = self.pipeline.review_reader.get_review_data(pull_request.number)
            if not isinstance(data, dict) or (
                data.get("number") != pull_request.number
                or data.get("url") != pull_request.url
                or data.get("headRefName") != pull_request.head
                or data.get("baseRefName") != self.config.github.pull_request_base
                or data.get("state") != "OPEN"
            ):
                raise RecoveryError("Pull Request divergiu durante a reconciliação")
            observed = data.get("headRefOid")
            if observed == expected_head:
                return PullRequest(pull_request.number, pull_request.url, "", pull_request.base, pull_request.head, observed)
            if observed != previous_head:
                raise RecoveryError("Pull Request aponta para SHA incompatível")
            if attempt < 2:
                time.sleep(min(self.config.ci.poll_interval_seconds, 1))
        raise RecoveryError("Pull Request não propagou o HEAD publicado no prazo limitado")

    def _refresh_review(self, record: RunRecord, worktree: GitWorktree | None, pull_request: PullRequest | None) -> RunRecord:
        if worktree is None or pull_request is None or not record.current_head_sha or self.pipeline.ci_reader is None:
            raise RecoveryError("Contexto insuficiente para refazer a revisão")
        ci = CiGate(self.pipeline.ci_reader, self.config.ci).wait(pull_request.number, record.current_head_sha)
        issue = self.pipeline.issue_reader.get_issue(record.issue_number)
        review = self.pipeline._review_head(issue, worktree, pull_request, record.current_head_sha, (), ci, ())
        if review.reviewed_head_sha != record.current_head_sha:
            raise RecoveryError("Reviewer retornou resultado para HEAD diferente")
        if review.verdict is ReviewVerdict.REJECTED:
            self._pending_rejected_review = review
            return self._transition(ExecutionPhase.NEEDS_CHANGES, "Review fresco rejeitou o HEAD persistido", head_sha=record.current_head_sha, reviewed_head_sha=review.reviewed_head_sha, review_verdict=review.verdict.value)
        if self.config.execution.auto_merge:
            return self._transition(ExecutionPhase.MERGING, "Review fresco aprovado; merge requer nova revalidação", head_sha=record.current_head_sha, reviewed_head_sha=review.reviewed_head_sha, review_verdict=review.verdict.value)
        return self._transition(ExecutionPhase.APPROVED_AWAITING_ACTION, "Review fresco aprovado; aguardando ação externa", head_sha=record.current_head_sha, reviewed_head_sha=review.reviewed_head_sha, review_verdict=review.verdict.value)

    def _resume_correction(self, record: RunRecord, worktree: GitWorktree | None, pull_request: PullRequest | None) -> RunRecord:
        if worktree is None or pull_request is None or not record.codex_session_id or not record.current_head_sha or self.pipeline.ci_reader is None:
            raise RecoveryError("Contexto insuficiente para retomar a correção")
        if record.correction_attempts >= self.config.review.max_correction_attempts:
            raise RecoveryError("Limite de correções atingido; nenhuma nova sessão ou publicação foi iniciada")
        issue = self.pipeline.issue_reader.get_issue(record.issue_number)
        review = self._pending_rejected_review
        if review is None or review.reviewed_head_sha != record.current_head_sha:
            ci = CiGate(self.pipeline.ci_reader, self.config.ci).wait(pull_request.number, record.current_head_sha)
            review = self.pipeline._review_head(issue, worktree, pull_request, record.current_head_sha, (), ci, ())
        if review.verdict is not ReviewVerdict.REJECTED:
            raise RecoveryError("Os findings persistidos não puderam ser reconstruídos como rejeitados")
        self._pending_rejected_review = None
        prompt = CorrectionContextBuilder().build(issue, pull_request.number, pull_request.url, record.current_head_sha, review, ())
        attempts = record.correction_attempts + 1
        self._transition(
            ExecutionPhase.CODEX_RUNNING, "Sessão Codex será retomada para correção",
            correction_attempts=attempts,
        )
        execution = self.pipeline.codex_executor.resume(worktree.path, record.codex_session_id, prompt)
        if execution.session_id != record.codex_session_id:
            raise RecoveryError("Codex retornou uma sessão diferente da sessão persistida")
        return self._transition(ExecutionPhase.TESTING, "Correção Codex concluída")

    def _reconcile_merge(self, record: RunRecord, worktree: GitWorktree | None, pull_request: PullRequest | None) -> RunRecord:
        if pull_request is None or self.pipeline.pull_request_merger is None or not record.reviewed_head_sha:
            raise RecoveryError("Dados insuficientes para reconciliar merge")
        snapshot = self.pipeline.pull_request_merger.get_merge_snapshot(pull_request.number)
        if snapshot.merged and snapshot.state == "MERGED" and snapshot.head_sha == record.reviewed_head_sha and snapshot.merge_commit_sha:
            self.pipeline.pull_request_merger.verify_merge_commit(snapshot.merge_commit_sha, record.reviewed_head_sha)
            record = self._checkpoint("Merge remoto reconciliado", record.reviewed_head_sha, merge_commit_sha=snapshot.merge_commit_sha, merged_head_sha=record.reviewed_head_sha)
            return self._complete_project(record, snapshot.merge_commit_sha, record.reviewed_head_sha)
        if snapshot.state != "OPEN" or worktree is None or self.pipeline.ci_reader is None or self.pipeline.git_publisher is None:
            raise RecoveryError("Merge não confirmado de forma inequívoca; nenhum retry cego foi executado")
        # O gate inteiro é refeito a partir de leituras atuais antes da única mutação.
        ci = CiGate(self.pipeline.ci_reader, self.config.ci).wait(pull_request.number, record.reviewed_head_sha)
        issue = self.pipeline.issue_reader.get_issue(record.issue_number)
        review = self.pipeline._review_head(issue, worktree, pull_request, record.reviewed_head_sha, (), ci, ())
        branch, local_head = self.pipeline.git_publisher.merge_state(worktree.path)
        # A leitura usada pelo gate deve ser imediatamente anterior ao merge.
        snapshot = self.pipeline.pull_request_merger.get_merge_snapshot(pull_request.number)
        MergeGate().validate(snapshot, pull_request_number=pull_request.number, pull_request_url=pull_request.url,
                            base=self.config.github.pull_request_base, branch=branch, local_head=local_head,
                            review=review, ci_result=ci, blocking_severities=self.config.review.blocking_severities)
        merge = self.pipeline.pull_request_merger.merge(pull_request.number, record.reviewed_head_sha)
        confirmed = self.pipeline.pull_request_merger.get_merge_snapshot(pull_request.number)
        if (not confirmed.merged or confirmed.state != "MERGED" or confirmed.head_sha != record.reviewed_head_sha
                or confirmed.merge_commit_sha != merge.merge_commit_sha):
            raise RecoveryError("GitHub não confirmou o merge do HEAD aprovado")
        self.pipeline.pull_request_merger.verify_merge_commit(merge.merge_commit_sha, record.reviewed_head_sha)
        record = self._checkpoint("Merge confirmado na retomada", record.reviewed_head_sha,
                                  merge_commit_sha=merge.merge_commit_sha, merged_head_sha=record.reviewed_head_sha)
        return self._complete_project(record, merge.merge_commit_sha, record.reviewed_head_sha)

    def _complete_project(self, record: RunRecord, merge_commit_sha: str, merged_head_sha: str) -> RunRecord:
        if not record.project_item_id:
            raise RecoveryError("Item do Project ausente após merge confirmado")
        project_done = False
        try:
            items = self.pipeline.project_reader.list_items()
            project_done = any(item.id == record.project_item_id and item.status == "Done" for item in items)
        except Exception as error:
            raise RecoveryError(f"Não foi possível revalidar o Project após merge: {error}") from error
        if not project_done:
            self.pipeline.status_writer.set_status(record.project_item_id, "Done")
        return self._transition(ExecutionPhase.COMPLETED, "Merge e Project Done reconciliados", merge_commit_sha=merge_commit_sha, merged_head_sha=merged_head_sha, project_status="Done")

    def _checkpoint(self, summary: str, head_sha: str | None = None, **updates: object) -> RunRecord:
        return self.store.checkpoint(self.pipeline._execution_id or "", summary=summary, head_sha=head_sha, **updates)

    def _transition(self, phase: ExecutionPhase, summary: str, **updates: object) -> RunRecord:
        return self.store.transition(self.pipeline._execution_id or "", phase, summary=summary, **updates)
