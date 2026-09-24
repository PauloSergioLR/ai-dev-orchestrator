"""Regressão #96/#97: retry limitado com identidade e evidências duráveis."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ai_dev_orchestrator.adapters.github import PullRequest
from ai_dev_orchestrator.domain.execution import ExecutionPhase as Phase
from ai_dev_orchestrator.domain.project import ProjectItem
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind as Kind
from ai_dev_orchestrator.domain.recovery import (
    CiObservation, CiState, MergeObservation, MergeState, PullRequestObservation,
    PullRequestState, RecoveryObservation, RecoveryPolicy, WorktreeState,
)
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.recovery_effects import RecoveryEffects
from ai_dev_orchestrator.services.recovery_executor import RecoveryExecutor
from ai_dev_orchestrator.services.recovery_planner import RecoveryPlanner
from ai_dev_orchestrator.services.resume import ResumeError, ResumeService
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA
from test_review_loop import LoopFakes, SHA_A, pipeline


class ProtocolFakes(LoopFakes):
    def __init__(self, outputs, *, fail_planner=False):
        super().__init__(rejected_reviews=0)
        self.outputs = iter(outputs)
        self.prompts = []
        self.fail_planner = fail_planner

    def list_items(self):
        return (ProjectItem("item", "Issue", 96, "Título", "url", "acme/repo", "Ready", None, None, None, None),)

    def create(self, issue, branch, gates):
        self.events.append("criar-pr")
        return PullRequest(97, "https://github.com/acme/repo/pull/97", issue.title, "main", branch)

    def get_review_data(self, number):
        return {**super().get_review_data(number), "number": 97, "url": "https://github.com/acme/repo/pull/97"}

    def get_merge_snapshot(self, number):
        return replace(super().get_merge_snapshot(number), number=97, url="https://github.com/acme/repo/pull/97")

    def invoke(self, prompt, cwd, schema):
        self.prompts.append((prompt, schema))
        if (schema == REVIEW_PLAN_SCHEMA) != self.fail_planner:
            return super().invoke(prompt, cwd, schema)
        output = next(self.outputs)
        if isinstance(output, BaseException):
            raise output
        return output if output is not None else super().invoke(prompt, cwd, schema)


def setup(tmp_path, outputs, *, store_type=SqliteExecutionStore, fail_planner=False):
    fakes = ProtocolFakes(outputs, fail_planner=fail_planner)
    service = pipeline(tmp_path, fakes)
    service.execution_store = store_type(tmp_path / "state.db")
    return service, fakes


def assert_identity(store, run, *, attempts=1):
    assert run.issue_number == 96 and run.pull_request_number == 97
    assert run.current_head_sha == run.ci_head_sha == SHA_A
    assert run.review_protocol_retry_head_sha == SHA_A
    assert run.correction_attempts == run.local_gate_correction_attempts == run.ci_correction_attempts == 0
    assert run.review_protocol_retry_attempts == attempts
    assert len(store.list_history()) == 1


@pytest.mark.parametrize("fail_planner", [False, True])
def test_invalid_then_valid_uses_exact_same_request_and_separate_budget(tmp_path, fail_planner):
    service, fakes = setup(tmp_path, ["{", None], fail_planner=fail_planner)
    result = service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, run)
    assert run.phase == Phase.APPROVED_AWAITING_ACTION
    assert run.last_error is None and run.quota_classification is None
    assert run.reviewed_head_sha == SHA_A and run.review_verdict == "APPROVED"
    assert result.review_attempts == 1
    index = 0 if fail_planner else 1
    assert fakes.prompts[index] == fakes.prompts[index + 1]
    assert fakes.events.count("gates") == fakes.events.count("execute") == fakes.events.count("criar-pr") == 1
    assert fakes.events.count(f"ci:{SHA_A}") == 1
    assert not fakes.resume_prompts


def test_two_invalid_responses_escalate_without_persisting_raw_output(tmp_path):
    secret = "resposta privada inteira token=SUPER_SECRET_99"
    service, fakes = setup(tmp_path, [secret, secret])
    with pytest.raises(RunPipelineError):
        service.run(96, "feat/review-loop")
    store = SqliteExecutionStore(service.execution_store.database_path)
    run = store.get_latest_for_issue(96)
    assert_identity(store, run)
    assert run.phase == Phase.HUMAN_REQUIRED and run.human_phase == "GEMINI_REVIEWING"
    assert run.quota_classification == "PROTOCOL_MALFORMED_RESPONSE"
    assert "protocol=JSON_INVALID" in run.last_error
    assert run.reviewed_head_sha is None and run.review_verdict is None
    assert store.review_findings(run.id) == ()
    assert len(fakes.prompts) == 3
    assert "SUPER_SECRET_99" not in repr(run) + repr(store.events(run.id))
    assert b"SUPER_SECRET_99" not in store.database_path.read_bytes()


class InterruptBeforeRetry(SqliteExecutionStore):
    def checkpoint(self, *args, **kwargs):
        run = super().checkpoint(*args, **kwargs)
        if run.review_checkpoint_json and json.loads(run.review_checkpoint_json)["status"] == "retry_pending":
            raise KeyboardInterrupt
        return run


def resume(service, fakes):
    store = SqliteExecutionStore(service.execution_store.database_path)
    class Observer:
        def observe(self, run):
            return RecoveryObservation(
                WorktreeState.CONVERGENT, local_head_sha=fakes.local_head,
                remote_head_sha=fakes.head,
                pull_requests=(PullRequestObservation(97, run.pull_request_url, "acme/repo", "main",
                               run.branch, fakes.head, PullRequestState.OPEN),),
                ci=CiObservation(CiState.SUCCESS, fakes.head), merge=MergeObservation(MergeState.OPEN),
            )
    effects = RecoveryEffects(service.config, projects=fakes)
    for name in ("codex", "validation", "publication", "issues", "pull_requests", "reviewer"):
        setattr(effects, name, fakes)
    def forbidden(*args):
        pytest.fail("Retry não pode executar gates ou consultar CI novamente")
    effects._validate = effects._wait_ci_result = forbidden
    policy = RecoveryPolicy("acme/repo", "main", False, 3)
    return store, ResumeService(store, Observer(), RecoveryPlanner(policy), RecoveryExecutor(policy, store, effects))


@pytest.mark.parametrize("second", [None, "{"])
def test_restart_after_failure_preserves_counter_context_and_identity(tmp_path, second):
    service, fakes = setup(tmp_path, ["{", second], store_type=InterruptBeforeRetry)
    with pytest.raises(KeyboardInterrupt):
        service.run(96, "feat/review-loop")
    original = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, original)
    assert original.phase == Phase.GEMINI_REVIEWING and original.reviewed_head_sha is None
    assert len(fakes.prompts) == 2
    store, recovery = resume(service, fakes)
    if second is None:
        recovery.resume(96)
    else:
        with pytest.raises(ResumeError):
            recovery.resume(96)
    final = store.get(original.id)
    assert_identity(store, final)
    assert final.phase == (Phase.APPROVED_AWAITING_ACTION if second is None else Phase.HUMAN_REQUIRED)
    assert fakes.prompts[1] == fakes.prompts[2]
    assert fakes.events.count("gates") == fakes.events.count("execute") == fakes.events.count("criar-pr") == 1
    assert not fakes.resume_prompts


@pytest.mark.parametrize("kind, phase", [
    (Kind.NETWORK_ERROR, Phase.WAITING_PROVIDER),
    (Kind.TERMINAL_QUOTA, Phase.WAITING_GEMINI_QUOTA),
    (Kind.AUTH_ERROR, Phase.HUMAN_REQUIRED),
    (Kind.MODEL_UNAVAILABLE, Phase.HUMAN_REQUIRED),
])
def test_provider_errors_keep_their_own_policy(tmp_path, kind, phase):
    error = ProviderFailure("gemini", kind, "token=privado", datetime.now(timezone.utc))
    service, fakes = setup(tmp_path, [error])
    service.config.supervisor.retry_without_reset_seconds = 60
    with pytest.raises(RunPipelineError):
        service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, run, attempts=0)
    assert run.phase == phase and run.quota_classification == kind.value
    assert run.provider_retry_attempts == 1 and len(fakes.prompts) == 2


@pytest.mark.parametrize("change, kind", [
    ({"reviewed_head_sha": "b" * 40}, "PROTOCOL_HEAD_MISMATCH"),
    ({"findings": [{"severity": "HIGH", "title": "Erro", "description": "Falha"}]}, "PROTOCOL_SEMANTIC_INVALID"),
])
def test_head_and_semantic_mismatch_stop_immediately(tmp_path, change, kind):
    output = json.dumps({"verdict": "APPROVED", "findings": [], "reviewed_head_sha": SHA_A, "summary": "ok", **change})
    service, fakes = setup(tmp_path, [output])
    with pytest.raises(RunPipelineError):
        service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, run, attempts=0)
    assert run.phase == Phase.HUMAN_REQUIRED and run.quota_classification == kind
    assert run.reviewed_head_sha is None and len(fakes.prompts) == 2


def test_prepared_request_redacts_secrets_without_breaking_json(tmp_path, monkeypatch):
    secret = "credencial-de-teste-99"
    monkeypatch.setenv("REVIEW_TEST_SECRET", secret)
    service, fakes = setup(tmp_path, ["{", None])
    original_invoke = fakes.invoke

    def plan_with_secret(prompt, cwd, schema):
        if schema == REVIEW_PLAN_SCHEMA:
            fakes.prompts.append((prompt, schema))
            return json.dumps({field: [f"token={secret}"] for field in schema["required"]})
        return original_invoke(prompt, cwd, schema)

    monkeypatch.setattr(fakes, "invoke", plan_with_secret)
    service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert run.review_verdict == "APPROVED"
    assert secret not in run.review_checkpoint_json
    assert secret not in repr(fakes.prompts)
    assert fakes.prompts[1] == fakes.prompts[2]


class InterruptBeforeHuman(SqliteExecutionStore):
    def require_human(self, *args, **kwargs):
        raise KeyboardInterrupt


def test_restart_finishes_escalation_with_original_diagnostic(tmp_path):
    output = json.dumps({"verdict": "APPROVED", "findings": [], "reviewed_head_sha": "b" * 40, "summary": "ok"})
    service, fakes = setup(tmp_path, [output], store_type=InterruptBeforeHuman)
    with pytest.raises(KeyboardInterrupt):
        service.run(96, "feat/review-loop")
    original = service.execution_store.get_latest_for_issue(96)
    assert original.phase == Phase.GEMINI_REVIEWING
    assert original.quota_classification == "PROTOCOL_HEAD_MISMATCH"
    store, recovery = resume(service, fakes)
    with pytest.raises(ResumeError):
        recovery.resume(96)
    final = store.get(original.id)
    assert final.phase == Phase.HUMAN_REQUIRED
    assert final.quota_classification == "PROTOCOL_HEAD_MISMATCH"
    assert "protocol=HEAD_MISMATCH" in final.last_error
    assert len(fakes.prompts) == 2
