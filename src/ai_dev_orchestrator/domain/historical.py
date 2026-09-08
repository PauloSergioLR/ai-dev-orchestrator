"""Reconciliação explícita de FAILED legado, com provas antes de qualquer efeito."""

from dataclasses import replace

from ai_dev_orchestrator.domain.execution import ExecutionPhase, RunRecord
from ai_dev_orchestrator.domain.provider import ProviderFailureKind
from ai_dev_orchestrator.domain.recovery import (
    RecoveryAction, WorktreeState, MergeState, ProjectState, CiState,
)


HISTORICAL_TRANSIENT_KINDS = frozenset({
    ProviderFailureKind.NETWORK_ERROR.value, ProviderFailureKind.TIMEOUT.value,
    ProviderFailureKind.LOCAL_TRANSIENT.value,
})


def is_historical_candidate(run: RunRecord) -> bool:
    return run.phase == ExecutionPhase.FAILED and run.quota_classification in HISTORICAL_TRANSIENT_KINDS


def historical_target(run, events):
    if not is_historical_candidate(run):
        raise ValueError("FAILED não possui classificação transitória conhecida")
    transitions = [e for e in events if e.phase == ExecutionPhase.FAILED and e.previous_phase != e.phase]
    if len(transitions) != 1:
        raise ValueError("Journal não prova uma única origem da falha")
    target = transitions[0].previous_phase
    if target not in {ExecutionPhase.CODEX_RUNNING, ExecutionPhase.GEMINI_REVIEWING, ExecutionPhase.TESTING}:
        raise ValueError("Fase histórica incompatível com retry seguro")
    expected_provider = "codex" if target == ExecutionPhase.CODEX_RUNNING else ("gemini" if target == ExecutionPhase.GEMINI_REVIEWING else "local")
    if run.quota_provider != expected_provider or run.quota_observed_at is None:
        raise ValueError("Metadados da falha histórica não correspondem à fase")
    return target


def validate_historical(run, target, observed, planner):
    if not all((run.branch, run.worktree_path, run.base_ref, run.codex_session_id,
                run.pull_request_number, run.pull_request_url, run.current_head_sha,
                run.project_item_id, run.project_status)):
        raise ValueError("Identidade histórica incompleta")
    if observed.worktree_state != WorktreeState.CONVERGENT:
        raise ValueError("Worktree histórico não converge")
    if observed.local_head_sha != run.current_head_sha or observed.remote_head_sha != run.current_head_sha:
        raise ValueError("HEAD local/remoto diverge do checkpoint histórico")
    if observed.merge.state != MergeState.OPEN or run.merge_commit_sha or run.merged_head_sha:
        raise ValueError("PR já fechado/merged ou merge desconhecido; intervenção necessária")
    if not planner._has_convergent_persisted_pr(run, observed):
        raise ValueError("PR histórico não converge")
    if run.issue_number not in observed.pull_requests[0].issue_numbers:
        raise ValueError("PR não comprova vínculo com a Issue")
    if observed.issue_number != run.issue_number or observed.issue_state != "OPEN":
        raise ValueError("Issue histórica não está aberta ou diverge")
    if observed.codex_session_id != run.codex_session_id:
        raise ValueError("Sessão local não comprova identidade/worktree")
    if observed.project_state != ProjectState.NOT_DONE or observed.project_status != run.project_status:
        raise ValueError("Project histórico desconhecido ou divergente")
    if run.ci_head_sha is not None and run.ci_head_sha != run.current_head_sha:
        raise ValueError("CI persistida pertence a outro HEAD")
    if target == ExecutionPhase.GEMINI_REVIEWING and (
        run.ci_head_sha != run.current_head_sha or observed.ci.head_sha != run.current_head_sha
        or observed.ci.state != CiState.SUCCESS
    ):
        raise ValueError("CI remota não comprova o HEAD da revisão histórica")
    if (run.review_verdict is None) != (run.reviewed_head_sha is None):
        raise ValueError("Review histórica parcialmente persistida")
    if run.review_verdict not in {None, "APPROVED", "REJECTED"}:
        raise ValueError("Veredito histórico desconhecido")
    if target == ExecutionPhase.CODEX_RUNNING and run.review_verdict != "REJECTED":
        raise ValueError("Correção histórica não possui review rejeitada")
    if run.review_verdict is not None and run.reviewed_head_sha != run.current_head_sha:
        raise ValueError("Review persistida pertence a outro HEAD")
    if run.review_verdict == "REJECTED" and observed.findings_head_sha != run.reviewed_head_sha:
        raise ValueError("Findings históricos ausentes ou divergentes")
    candidate = replace(run, phase=target)
    decision = planner.plan(candidate, observed)
    if decision.action == RecoveryAction.BLOCK:
        raise ValueError(decision.reason)
