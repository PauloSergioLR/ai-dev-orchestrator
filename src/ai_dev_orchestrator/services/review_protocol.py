"""Retry de protocolo por HEAD, com entrada congelada e reserva durável."""

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.provider import (
    FAILURE_MESSAGES, ProviderFailure, ProviderFailureKind as Kind,
    sanitized_diagnostic_context,
)
from ai_dev_orchestrator.infrastructure.redaction import redact_secrets
from ai_dev_orchestrator.services.review import (
    REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA, ReviewError, ReviewProtocolError,
    build_checklists, parse_review_plan, parse_structured_review, untrusted_json,
)


MAX_PROTOCOL_RETRIES = 1
RETRYABLE_PROTOCOL_FAILURES = frozenset({
    Kind.PROTOCOL_MALFORMED_RESPONSE, Kind.PROTOCOL_SCHEMA_MISMATCH,
})
PROTOCOL_FAILURES = RETRYABLE_PROTOCOL_FAILURES | {
    Kind.PROTOCOL_ERROR, Kind.PROTOCOL_HEAD_MISMATCH,
    Kind.PROTOCOL_CLI_INCOMPATIBLE, Kind.PROTOCOL_SEMANTIC_INVALID,
}


def _redact_data(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_data(item) for item in value]
    return value


def _prompt_parts(prompt):
    prefix, data = prompt.split("<DADOS_NAO_CONFIAVEIS>\n", 1)
    payload, suffix = data.split("\n</DADOS_NAO_CONFIAVEIS>", 1)
    return prefix, json.loads(payload), suffix


def _safe_prompt(prefix, payload, suffix):
    return (redact_secrets(prefix) + "<DADOS_NAO_CONFIAVEIS>\n"
            + untrusted_json(_redact_data(payload)) + "\n</DADOS_NAO_CONFIAVEIS>" + suffix)


class ReviewProtocolSession:
    """Compartilha um único orçamento entre planner e reviewer do mesmo HEAD."""

    def __init__(self, reviewer, *, store, execution_id, identity, blocking, configuration):
        self.reviewer, self.store, self.execution_id = reviewer, store, execution_id
        self.identity, self.blocking = identity, blocking
        self.fingerprint = sha256(json.dumps({
            "schemas": [REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA],
            "blocking": blocking, "configuration": configuration,
        }, sort_keys=True).encode()).hexdigest()
        self.state = None
        self.attempts = 0

    def _save(self, summary, **updates):
        if self.store is not None and self.execution_id is not None:
            self.store.checkpoint(
                self.execution_id, summary=summary,
                review_checkpoint_json=json.dumps(self.state, ensure_ascii=False),
                review_protocol_retry_attempts=self.attempts,
                review_protocol_retry_head_sha=self.identity["head_sha"], **updates,
            )

    def _stop(self, classification, diagnostic=None):
        detail = f"{classification.value}: {FAILURE_MESSAGES[classification]}"
        diagnostic = sanitized_diagnostic_context(diagnostic)
        if diagnostic:
            detail += f"; {diagnostic}"
        self.state.update(status="blocked", classification=classification.value, diagnostic=diagnostic)
        self._save(
            detail, last_error=detail, quota_provider="gemini",
            quota_classification=classification.value,
            quota_observed_at=datetime.now(timezone.utc).isoformat(),
            quota_retry_at=None, provider_resume_phase=ExecutionPhase.GEMINI_REVIEWING.value,
        )
        if self.store is not None and self.execution_id is not None:
            self.store.require_human(self.execution_id, summary=detail, reason=classification.value)
        raise ReviewError(detail)

    def _ensure_identity(self, ensure_identity):
        try:
            ensure_identity()
        except ReviewProtocolError as error:
            self._stop(error.classification, error.diagnostic_context)
        except ReviewError:
            self._stop(Kind.PROTOCOL_HEAD_MISMATCH)

    def run(self, prepare_prompt, ensure_identity):
        if self.store is not None and self.execution_id is not None:
            run = self.store.get(self.execution_id)
            if run.phase != ExecutionPhase.GEMINI_REVIEWING or run.current_head_sha != self.identity["head_sha"]:
                raise ReviewError("Checkpoint incompatível com o HEAD da revisão")
            if run.review_protocol_retry_head_sha == self.identity["head_sha"]:
                self.attempts = run.review_protocol_retry_attempts
                if run.review_checkpoint_json:
                    self.state = json.loads(run.review_checkpoint_json)
        if self.state is None:
            self.state = {"version": 1, "identity": self.identity,
                          "fingerprint": self.fingerprint, "stage": "planner", "status": "ready",
                          "prompt": _safe_prompt(*_prompt_parts(prepare_prompt()))}
            self._save("Entrada sanitizada de review congelada para o HEAD")
        if self.state.get("identity") != self.identity:
            self._stop(Kind.PROTOCOL_HEAD_MISMATCH)
        if self.state.get("version") != 1 or self.state.get("fingerprint") != self.fingerprint:
            self._stop(Kind.PROTOCOL_CLI_INCOMPATIBLE)
        if self.state.get("status") == "completed":
            # A resposta já foi validada antes do checkpoint. Reparseá-la preserva
            # o fail-closed e cobre queda entre a validação e record_review.
            self._ensure_identity(ensure_identity)
            return parse_structured_review(
                json.dumps(self.state.get("result")), self.identity["head_sha"], self.blocking
            )
        # Um processo interrompido pode ter consumido a chamada reservada. Não o repete.
        if self.state.get("status") == "blocked":
            self._stop(Kind(self.state["classification"]), self.state.get("diagnostic"))
        if self.state.get("status") not in {"ready", "retry_pending"}:
            self._stop(Kind.PROTOCOL_ERROR)
        while True:
            self._ensure_identity(ensure_identity)
            self.state["status"] = "in_flight"
            self._save("Chamada estruturada reservada antes de invocar o reviewer")
            stage = self.state["stage"]
            schema = REVIEW_PLAN_SCHEMA if stage == "planner" else STRUCTURED_REVIEW_SCHEMA
            try:
                output = self.reviewer.invoke(
                    self.state["prompt"], Path(self.identity["worktree"]), schema
                )
                parsed = (parse_review_plan(output) if stage == "planner" else
                          parse_structured_review(output, self.identity["head_sha"], self.blocking))
            except (ProviderFailure, ReviewProtocolError) as error:
                kind = error.classification
                if isinstance(error, ProviderFailure) and (error.provider != "gemini" or kind not in PROTOCOL_FAILURES):
                    # Auth/quota/network continuam na política própria, sem consumir protocolo.
                    self.state["status"] = "ready"
                    self._save("Chamada suspensa pela política própria do provider")
                    raise
                diagnostic = sanitized_diagnostic_context(error.diagnostic_context)
                if kind not in RETRYABLE_PROTOCOL_FAILURES or self.attempts >= MAX_PROTOCOL_RETRIES:
                    self._stop(kind, diagnostic)
                self.attempts += 1
                self.state["status"] = "retry_pending"
                detail = f"{kind.value}: {FAILURE_MESSAGES[kind]}"
                if diagnostic:
                    detail += f"; {diagnostic}"
                self._save(
                    detail + "; retry de protocolo reservado (1/1)", last_error=detail,
                    quota_provider="gemini", quota_classification=kind.value,
                    quota_observed_at=datetime.now(timezone.utc).isoformat(), quota_retry_at=None,
                )
                continue
            self._ensure_identity(ensure_identity)
            if stage == "reviewer":
                self.state.update(status="completed", result=_redact_data(asdict(parsed)))
                self._save(
                    "Review estruturada validada integralmente", last_error=None,
                    quota_provider=None, quota_classification=None, quota_observed_at=None,
                    quota_retry_at=None, provider_resume_phase=None,
                )
                return parsed
            # Só dados validados e redigidos entram no próximo request; nunca o stdout.
            prompt = self.state["prompt"]
            prefix, payload, _ = _prompt_parts(prompt)
            payload["review_plan"] = asdict(parsed)
            payload["checklists"] = build_checklists(tuple(payload["dossier"]["changed_files"]))
            self.state.update(
                stage="reviewer", status="ready",
                prompt=_safe_prompt(prefix, payload, "\n\nProduza somente JSON do StructuredReview."),
            )
            self._save("Plano validado; entrada sanitizada da análise final congelada")
