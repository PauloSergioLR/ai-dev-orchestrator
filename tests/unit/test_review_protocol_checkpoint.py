"""Checkpoint de protocolo conserva o limite diante de interrupções e divergências."""

import json

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase as Phase
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import RunPipelineError
from ai_dev_orchestrator.services.resume import ResumeError
from ai_dev_orchestrator.services.review import REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA
from test_review_loop import SHA_A, SHA_B
from test_review_protocol_retry import InterruptBeforeRetry, assert_identity, resume, setup


class InterruptDuringRetry(SqliteExecutionStore):
    def checkpoint(self, *args, **kwargs):
        run = super().checkpoint(*args, **kwargs)
        if (
            run.review_protocol_retry_attempts == 1
            and run.review_checkpoint_json
            and json.loads(run.review_checkpoint_json)["status"] == "in_flight"
        ):
            raise KeyboardInterrupt
        return run


class InterruptAfterValidatedReview(SqliteExecutionStore):
    def checkpoint(self, *args, **kwargs):
        run = super().checkpoint(*args, **kwargs)
        if (
            run.review_checkpoint_json
            and json.loads(run.review_checkpoint_json)["status"] == "completed"
        ):
            raise KeyboardInterrupt
        return run


def test_restart_with_retry_in_flight_stops_without_another_provider_call(tmp_path):
    service, fakes = setup(tmp_path, ["{", None], store_type=InterruptDuringRetry)
    with pytest.raises(KeyboardInterrupt):
        service.run(96, "feat/review-loop")
    original = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, original)
    assert original.phase == Phase.GEMINI_REVIEWING
    assert json.loads(original.review_checkpoint_json)["status"] == "in_flight"
    assert len(fakes.prompts) == 2

    store, recovery = resume(service, fakes)
    with pytest.raises(ResumeError):
        recovery.resume(96)
    final = store.get(original.id)
    assert_identity(store, final)
    assert final.phase == Phase.HUMAN_REQUIRED
    assert final.human_phase == "GEMINI_REVIEWING"
    assert final.quota_classification == "PROTOCOL_ERROR"
    assert final.reviewed_head_sha is None and final.review_verdict is None
    assert store.review_findings(final.id) == ()
    assert len(fakes.prompts) == 2
    with pytest.raises(ResumeError):
        recovery.resume(96, retry_provider=True)
    assert len(fakes.prompts) == 2
    assert store.get(original.id).review_protocol_retry_attempts == 1


def test_restart_after_validation_records_cached_review_without_provider_or_gates(tmp_path):
    service, fakes = setup(tmp_path, [None], store_type=InterruptAfterValidatedReview)
    with pytest.raises(KeyboardInterrupt):
        service.run(96, "feat/review-loop")
    original = service.execution_store.get_latest_for_issue(96)
    checkpoint = json.loads(original.review_checkpoint_json)
    assert checkpoint["status"] == "completed"
    assert checkpoint["result"]["reviewed_head_sha"] == SHA_A
    assert original.phase == Phase.GEMINI_REVIEWING and original.reviewed_head_sha is None
    calls = len(fakes.prompts)

    store, recovery = resume(service, fakes)
    result = recovery.resume(96)
    final = store.get(original.id)
    assert result.phase == Phase.APPROVED_AWAITING_ACTION.value
    assert final.reviewed_head_sha == SHA_A and final.review_verdict == "APPROVED"
    assert len(fakes.prompts) == calls
    assert fakes.events.count("gates") == fakes.events.count("execute") == 1


def test_remote_head_changes_before_retry_block_without_second_invoke(tmp_path, monkeypatch):
    service, fakes = setup(tmp_path, ["{", None])
    invoke = fakes.invoke

    def change_remote_head(prompt, cwd, schema):
        result = invoke(prompt, cwd, schema)
        if schema == STRUCTURED_REVIEW_SCHEMA:
            fakes.head = SHA_B
        return result

    monkeypatch.setattr(fakes, "invoke", change_remote_head)
    with pytest.raises(RunPipelineError):
        service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, run)
    assert run.phase == Phase.HUMAN_REQUIRED
    assert run.quota_classification == "PROTOCOL_HEAD_MISMATCH"
    assert run.reviewed_head_sha is None and run.review_verdict is None
    assert fakes.head == SHA_B and fakes.local_head == SHA_A
    assert len(fakes.prompts) == 2
    assert fakes.events.count("gates") == fakes.events.count("execute") == 1
    assert not fakes.resume_prompts


@pytest.mark.parametrize("field, changed", [
    ("executable", "agy-alternativo"),
    ("blocking_severities", ("CRITICAL", "HIGH")),
])
def test_changed_configuration_after_restart_is_cli_incompatible(tmp_path, field, changed):
    service, fakes = setup(tmp_path, ["{", None], store_type=InterruptBeforeRetry)
    with pytest.raises(KeyboardInterrupt):
        service.run(96, "feat/review-loop")
    original = service.execution_store.get_latest_for_issue(96)
    frozen_prompt = json.loads(original.review_checkpoint_json)["prompt"]
    setattr(service.config.review, field, changed)

    store, recovery = resume(service, fakes)
    with pytest.raises(ResumeError):
        recovery.resume(96)
    final = store.get(original.id)
    assert_identity(store, final)
    assert final.phase == Phase.HUMAN_REQUIRED
    assert final.quota_classification == "PROTOCOL_CLI_INCOMPATIBLE"
    assert final.reviewed_head_sha is None and final.review_verdict is None
    assert json.loads(final.review_checkpoint_json)["prompt"] == frozen_prompt
    assert len(fakes.prompts) == 2


def test_planner_and_reviewer_share_the_single_protocol_retry(tmp_path, monkeypatch):
    service, fakes = setup(tmp_path, ["{", None, "{", None], fail_planner=True)
    invoke = fakes.invoke

    def fail_both_stages(prompt, cwd, schema):
        result = invoke(prompt, cwd, schema)
        if schema == REVIEW_PLAN_SCHEMA and len(fakes.prompts) == 2:
            fakes.fail_planner = False
        return result

    monkeypatch.setattr(fakes, "invoke", fail_both_stages)
    with pytest.raises(RunPipelineError):
        service.run(96, "feat/review-loop")
    run = service.execution_store.get_latest_for_issue(96)
    assert_identity(service.execution_store, run)
    assert run.phase == Phase.HUMAN_REQUIRED
    assert run.quota_classification == "PROTOCOL_MALFORMED_RESPONSE"
    assert run.reviewed_head_sha is None and run.review_verdict is None
    assert [schema for _prompt, schema in fakes.prompts] == [
        REVIEW_PLAN_SCHEMA, REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA,
    ]
    assert fakes.prompts[0] == fakes.prompts[1]
    assert fakes.events.count("gates") == fakes.events.count("execute") == 1
    assert fakes.events.count("criar-pr") == 1
    assert not fakes.resume_prompts
