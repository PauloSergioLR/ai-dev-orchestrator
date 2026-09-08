"""Política única de espera e intervenção para pipeline, resume e supervisor."""

from datetime import datetime, timedelta, timezone

from ai_dev_orchestrator.domain.execution import (
    ExecutionPhase, RunRecord, PROVIDER_WAIT_PHASES, RESUMABLE_PROVIDER_PHASES,
)
from ai_dev_orchestrator.domain.provider import (
    FAILURE_MESSAGES, FAILURE_POLICY, FailureDisposition, ProviderFailure,
    ProviderFailureKind,
)


RETRY_DELAYS_SECONDS = (30, 60, 120)


class ProviderRecoveryError(Exception):
    """O checkpoint exige intervenção antes de uma nova chamada."""


def record_provider_failure(store, execution_id: str, failure: ProviderFailure) -> RunRecord:
    # O executor pode ter persistido o início da correção antes de lançar a falha.
    run = store.get(execution_id)
    if run.phase not in RESUMABLE_PROVIDER_PHASES:
        raise ProviderRecoveryError("Fase incompatível com retry de provider")
    kind = failure.classification
    disposition = FAILURE_POLICY[kind]
    session = run.codex_session_id
    if failure.provider == "codex":
        if session and failure.session_id and failure.session_id != session:
            kind = ProviderFailureKind.PROTOCOL_ERROR
            disposition = FailureDisposition.INTERVENTION
        else:
            session = session or failure.session_id
        # Uma primeira chamada pode ter criado uma sessão antes de perder a saída.
        if not session:
            disposition = FailureDisposition.INTERVENTION
    attempts = run.provider_retry_attempts + 1
    retry_at = failure.retry_at
    if retry_at is not None and (retry_at.tzinfo is None or retry_at <= failure.observed_at):
        retry_at = None
    phase = ExecutionPhase.BLOCKED_PROVIDER
    if disposition == FailureDisposition.RETRY and attempts <= len(RETRY_DELAYS_SECONDS):
        retry_at = failure.observed_at + timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
        phase = ExecutionPhase.WAITING_PROVIDER
    elif disposition == FailureDisposition.WAIT_RESET:
        phase = (ExecutionPhase.WAITING_CODEX_QUOTA if failure.provider == "codex"
                 else ExecutionPhase.WAITING_GEMINI_QUOTA)
    else:
        retry_at = None
    source = failure.diagnostic_source if failure.diagnostic_source in {
        "provider", "processo", "JSONL", "JSON", "stdout", "stderr", "CLI", "thread.started", "protocolo",
    } else "provider"
    detail = (f"{kind.value}: {FAILURE_MESSAGES[kind]} "
              f"(exit={failure.returncode}, fonte={source}, tentativa={attempts})")
    if failure.provider == "codex" and not session:
        detail += "; sessão não comprovada; criação automática bloqueada"
    return store.transition(
        run.id, phase, summary=detail, last_error=detail,
        quota_provider=failure.provider if failure.provider in {"codex", "gemini", "local"} else "local",
        quota_classification=kind.value, quota_observed_at=failure.observed_at.isoformat(),
        quota_retry_at=retry_at.isoformat() if retry_at else None,
        provider_resume_phase=run.phase.value, provider_retry_attempts=attempts,
        codex_session_id=session,
    )


def resume_provider_wait(store, run: RunRecord, *, manual_retry: bool = False,
                         now: datetime | None = None) -> RunRecord:
    if run.phase not in PROVIDER_WAIT_PHASES:
        return run
    if run.phase == ExecutionPhase.BLOCKED_PROVIDER and not manual_retry:
        raise ProviderRecoveryError(f"{run.last_error}; intervenção necessária; use --retry-provider após corrigir a causa")
    if run.quota_retry_at is None and not manual_retry:
        raise ProviderRecoveryError("Provider não informou quando retentar; intervenção necessária (--retry-provider)")
    if run.quota_retry_at and run.quota_retry_at > (now or datetime.now(timezone.utc)):
        return run
    target = run.provider_resume_phase
    if target is None and run.phase in {ExecutionPhase.WAITING_CODEX_QUOTA, ExecutionPhase.WAITING_GEMINI_QUOTA}:
        target = (ExecutionPhase.CODEX_RUNNING if run.phase == ExecutionPhase.WAITING_CODEX_QUOTA
                  else ExecutionPhase.GEMINI_REVIEWING).value
    if target not in {p.value for p in RESUMABLE_PROVIDER_PHASES}:
        raise ProviderRecoveryError("Checkpoint de provider sem fase de retomada válida")
    if target == ExecutionPhase.CODEX_RUNNING and not run.codex_session_id:
        raise ProviderRecoveryError("Sessão Codex não comprovada; não é seguro criar outra sessão")
    return store.transition(
        run.id, ExecutionPhase(target), summary="Retry explícito após intervenção" if manual_retry else "Janela de retry alcançada",
        quota_provider=None, quota_classification=None, quota_observed_at=None,
        quota_retry_at=None, provider_resume_phase=None, last_error=None,
        provider_retry_attempts=0 if manual_retry else run.provider_retry_attempts,
    )
