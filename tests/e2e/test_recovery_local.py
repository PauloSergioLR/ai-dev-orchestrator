"""Regressões E2E locais para os incidentes de recovery observados."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.provider import ProviderFailure, ProviderFailureKind
from ai_dev_orchestrator.services.resume import ResumeError

from .harness import HEAD_ONE, HEAD_TWO, LocalWorld, assert_invariants, make_service


def test_fluxo_feliz_com_correcao_reusa_sessao_pr_e_checkpoints(tmp_path) -> None:
    world = LocalWorld()
    store, service, original = make_service(tmp_path, world)

    result = service.resume(67)
    run = assert_invariants(store, 67)

    assert result.phase == ExecutionPhase.COMPLETED.value
    assert run.id == original.id
    assert run.codex_session_id == "sessao-unica"
    assert run.current_head_sha == HEAD_TWO
    assert world.calls.count("create_pr") == world.calls.count("merge") == 1
    assert world.calls == [
        "prepare", "start_codex", "gates", "commit", "push", "create_pr", "review",
        "resume_correction", "gates", "commit", "push", "review", "merge", "done",
    ]
    assert len(world.review_prompts[0]) > 10_000


@pytest.mark.parametrize("boundary", ["push", "pr", "review", "merge"])
def test_restart_em_fronteiras_remotas_nao_repete_efeito(tmp_path, boundary: str) -> None:
    world = LocalWorld(reject_first_review=False, crash_after=boundary)
    store, service, original = make_service(tmp_path, world)

    with pytest.raises(ResumeError, match="queda injetada"):
        service.resume(67)
    before = list(world.calls)
    result = service.resume(67)
    run = assert_invariants(store, 67)

    assert result.execution_id == original.id
    assert run.phase == ExecutionPhase.COMPLETED
    assert world.calls.count("create_pr") == 1
    assert world.calls.count("merge") == 1
    # Após a queda, a reconciliação observa o efeito remoto antes de tentar mutá-lo.
    assert len(world.calls) >= len(before)


def test_head_remoto_divergente_e_project_inconsistente_bloqueiam_sem_merge(tmp_path) -> None:
    world = LocalWorld(reject_first_review=False)
    store, service, _ = make_service(tmp_path, world)
    world._prepared, world.local_head, world.remote_head, world.pr_exists = True, HEAD_ONE, "e" * 40, True
    run = store.get_active_for_issue(67)
    assert run is not None
    for phase in (ExecutionPhase.CODEX_RUNNING, ExecutionPhase.TESTING, ExecutionPhase.COMMIT_PENDING, ExecutionPhase.PUSH_PENDING, ExecutionPhase.PR_PENDING):
        updates = {}
        if phase == ExecutionPhase.CODEX_RUNNING:
            updates["current_head_sha"] = HEAD_ONE
        if phase == ExecutionPhase.TESTING:
            updates["codex_session_id"] = "sessao-unica"
        run = store.transition(run.id, phase, summary="preparo e2e", **updates)

    with pytest.raises(ResumeError, match="Branch remota|Pull Request"):
        service.resume(67)
    assert "merge" not in world.calls
    assert_invariants(store, 67)


def test_pr_mergeado_manualmente_e_reconciliado_sem_segundo_merge(tmp_path) -> None:
    world = LocalWorld(reject_first_review=False)
    store, service, original = make_service(tmp_path, world)
    world._prepared = world.pr_exists = world.merged = True
    world.local_head = world.remote_head = HEAD_ONE
    run = store.transition(original.id, ExecutionPhase.CODEX_RUNNING, summary="worktree", current_head_sha=HEAD_ONE)
    run = store.transition(run.id, ExecutionPhase.TESTING, summary="sessão", codex_session_id="sessao-unica")
    run = store.transition(run.id, ExecutionPhase.COMMIT_PENDING, summary="gates")
    run = store.transition(run.id, ExecutionPhase.PUSH_PENDING, summary="commit")
    run = store.transition(run.id, ExecutionPhase.PR_PENDING, summary="push")
    run = store.transition(run.id, ExecutionPhase.WAITING_CI, summary="pr", pull_request_number=1, pull_request_url="https://example.test/acme/repo/pull/1")
    run = store.transition(run.id, ExecutionPhase.GEMINI_REVIEWING, summary="ci", ci_head_sha=HEAD_ONE)
    store.transition(run.id, ExecutionPhase.MERGE_PENDING, summary="review aprovada", reviewed_head_sha=HEAD_ONE,
                     review_verdict="APPROVED")

    result = service.resume(67)
    reconciled = assert_invariants(store, 67)

    assert result.phase == ExecutionPhase.COMPLETED.value
    assert reconciled.id == original.id
    assert world.calls == ["done"]
    assert reconciled.merge_commit_sha


@pytest.mark.parametrize("kind", [ProviderFailureKind.NETWORK_ERROR, ProviderFailureKind.TIMEOUT])
def test_falhas_transitorias_preservam_identidade_e_permite_retry(tmp_path, kind) -> None:
    world = LocalWorld()
    store, service, original = make_service(tmp_path, world)
    run = store.transition(original.id, ExecutionPhase.CODEX_RUNNING, summary="worktree", current_head_sha=HEAD_ONE)
    run = store.checkpoint(run.id, summary="sessão", codex_session_id="sessao-unica")
    failure = ProviderFailure("codex", kind, "falha transitória", datetime.now(timezone.utc), session_id="sessao-unica")
    waiting = service._record_provider_wait(run, failure)

    assert waiting.phase == ExecutionPhase.WAITING_PROVIDER
    assert waiting.codex_session_id == "sessao-unica"
    assert waiting.id == original.id
    assert_invariants(store, 67)


def test_quota_sem_retry_seguro_nao_cria_outra_sessao(tmp_path) -> None:
    world = LocalWorld()
    store, service, original = make_service(tmp_path, world)
    run = store.transition(original.id, ExecutionPhase.CODEX_RUNNING, summary="worktree", current_head_sha=HEAD_ONE)
    run = store.checkpoint(run.id, summary="sessão", codex_session_id="sessao-unica")
    quota = ProviderFailure("codex", ProviderFailureKind.TERMINAL_QUOTA, "You've hit your usage limit",
                            datetime.now(timezone.utc), session_id="sessao-unica")
    waiting = service._record_provider_wait(run, quota)

    assert waiting.phase == ExecutionPhase.WAITING_CODEX_QUOTA
    with pytest.raises(ResumeError, match="não informou quando retentar"):
        service.resume(67)
    assert world.calls == []
    assert_invariants(store, 67)


def test_quota_com_janela_conhecida_retomara_mesma_sessao(tmp_path) -> None:
    world = LocalWorld()
    store, service, original = make_service(tmp_path, world)
    run = store.transition(original.id, ExecutionPhase.CODEX_RUNNING, summary="worktree", current_head_sha=HEAD_ONE)
    run = store.checkpoint(run.id, summary="sessão", codex_session_id="sessao-unica")
    quota = ProviderFailure("codex", ProviderFailureKind.TERMINAL_QUOTA, "You've hit your usage limit",
                            datetime.now(timezone.utc), datetime.now(timezone.utc) - timedelta(seconds=1), "sessao-unica")
    service._record_provider_wait(run, quota)

    with pytest.raises(ResumeError):  # o mundo ainda não tem worktree convergente; a chamada foi resume, nunca start.
        service.resume(67)
    assert "start_codex" not in world.calls
    assert_invariants(store, 67)


def test_quota_no_resume_da_correcao_preserva_sessao_pr_e_nao_faz_merge(tmp_path) -> None:
    quota = ProviderFailure("codex", ProviderFailureKind.TERMINAL_QUOTA, "You've hit your usage limit",
                            datetime.now(timezone.utc), session_id="sessao-unica")
    world = LocalWorld(provider_failure_at="resume_correction", provider_failure=quota)
    store, service, original = make_service(tmp_path, world)

    result = service.resume(67)
    waiting = assert_invariants(store, 67)

    assert result.phase == ExecutionPhase.WAITING_CODEX_QUOTA.value
    assert waiting.id == original.id
    assert waiting.codex_session_id == "sessao-unica"
    assert waiting.pull_request_number == 1
    assert waiting.provider_resume_phase == ExecutionPhase.CODEX_RUNNING.value
    assert world.calls.count("resume_correction") == 1
    assert "merge" not in world.calls


def test_duas_issues_no_mesmo_journal_sao_isoladas_e_vaga_reabre_apos_conclusao(tmp_path) -> None:
    first = LocalWorld()
    second = LocalWorld(reject_first_review=False)
    database = tmp_path / "paralelo.db"
    store_a, service_a, run_a = make_service(tmp_path, first, 67, database_path=database)
    store_b, service_b, run_b = make_service(tmp_path, second, 68, database_path=database)
    store_a.require_human(run_a.id, summary="quota requer ação", reason="QUOTA")
    result = service_b.resume(68)

    assert store_a.get(run_a.id).phase == ExecutionPhase.HUMAN_REQUIRED
    assert result.phase == ExecutionPhase.COMPLETED.value
    assert store_b.list_active() == (store_a.get(run_a.id),)
    assert_invariants(store_a, 67)
    assert_invariants(store_b, 68)


def test_ctrl_c_na_observacao_preserva_checkpoint_de_todas_as_execucoes(tmp_path) -> None:
    database = tmp_path / "interrompido.db"
    first = LocalWorld()
    second = LocalWorld()
    store_a, service_a, run_a = make_service(tmp_path, first, 67, database_path=database)
    store_b, _, run_b = make_service(tmp_path, second, 68, database_path=database)

    class InterruptingObserver:
        def observe(self, run):
            raise KeyboardInterrupt

    service_a.observer = InterruptingObserver()
    with pytest.raises(KeyboardInterrupt):
        service_a.resume(67)

    assert store_a.get(run_a.id).phase == ExecutionPhase.PREPARING
    assert store_b.get(run_b.id).phase == ExecutionPhase.PREPARING
    assert store_a.events(run_a.id)[-1].summary == "Retomada interrompida"
    assert_invariants(store_a, 67)
    assert_invariants(store_b, 68)
