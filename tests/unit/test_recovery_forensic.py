"""Regressões forenses de ownership, snapshots e provas de recuperação."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
from threading import Barrier, Event

import pytest

from ai_dev_orchestrator.domain.execution import ExecutionPhase
from ai_dev_orchestrator.domain.recovery import RecoveryAction, RecoveryObservation, WorktreeState
from ai_dev_orchestrator.infrastructure.database import (
    ActiveExecutionError, ExecutionStoreError, SqliteExecutionStore,
)
from ai_dev_orchestrator.infrastructure.ownership import OwnershipError, exclusive_ownership
from ai_dev_orchestrator.services.contract_recovery import ContractRecoveryPreview, ContractRecoveryService
from ai_dev_orchestrator.services.resume import ResumeError
from ai_dev_orchestrator.services.supersession import SupersessionService


def test_claims_concorrentes_respeitam_capacidade_global(tmp_path):
    store = SqliteExecutionStore(tmp_path / "capacity.db")
    barrier = Barrier(2)

    def claim(issue):
        barrier.wait(timeout=5)
        try:
            return store.create(issue, max_active_runs=1)
        except ActiveExecutionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(claim, (11, 12)))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_history()) == 1


def test_inicializacao_concorrente_nao_duplica_schema(tmp_path):
    path = tmp_path / "startup.db"
    barrier = Barrier(2)

    def initialize(_):
        barrier.wait(timeout=5)
        return SqliteExecutionStore(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = tuple(pool.map(initialize, range(2)))
    with stores[0]._connection() as connection:
        assert connection.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1


def _crash_with_lock(path: str, ready, release) -> None:
    with exclusive_ownership(Path(path)):
        ready.set()
        if not release.wait(15):
            os._exit(18)
        # Crash deliberado: nenhum finally de Python pode liberar o lock.
        os._exit(17)


def test_kernel_lock_excludes_another_process_and_releases_after_crash(tmp_path):
    path = tmp_path / "persistente.lock"
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_crash_with_lock, args=(str(path), ready, release))
    process.start()
    try:
        assert ready.wait(15)
        with pytest.raises(OwnershipError):
            with exclusive_ownership(path):
                pytest.fail("Dois processos adquiriram o mesmo recurso")
        release.set()
        process.join(15)
        assert process.exitcode == 17
        assert path.exists()
        with exclusive_ownership(path):
            pass
    finally:
        if process.is_alive():
            process.terminate()
        process.join(15)


def test_issue_ownership_is_reentrant_and_preserves_parallel_other_issues(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")

    def acquire(issue):
        with store.ownership(issue):
            return issue

    with store.ownership(1), store.ownership(1), ThreadPoolExecutor(1) as pool:
        assert pool.submit(acquire, 2).result(timeout=5) == 2
        with pytest.raises(OwnershipError):
            pool.submit(acquire, 1).result(timeout=5)
    assert acquire(1) == 1


def test_second_resume_is_rejected_before_observation_or_effect(tmp_path):
    from test_resume import Effects, Observer, advance, service, HEAD, pr
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = advance(store, ExecutionPhase.WAITING_CI)
    entered, release = Event(), Event()

    def snapshot(_run):
        entered.set()
        assert release.wait(5)
        return RecoveryObservation(
            WorktreeState.CONVERGENT, local_head_sha=HEAD, remote_head_sha=HEAD,
            pull_requests=(pr(),),
        )

    class WaitingEffects(Effects):
        def wait_for_ci(self, _run):
            from ai_dev_orchestrator.domain.recovery import CiObservation
            self.called("ci")
            return CiObservation()

    observer, effects = Observer(snapshot), WaitingEffects()
    first = service(store, observer, effects)
    second = service(SqliteExecutionStore(store.database_path), observer, effects)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(first.resume, run.issue_number)
        try:
            assert entered.wait(5)
            events = store.events(run.id)
            with pytest.raises(ResumeError, match="Outra operação"):
                second.resume(run.issue_number)
            assert store.events(run.id) == events
            assert observer.calls == 1 and effects.calls == {}
        finally:
            release.set()
        assert future.result(timeout=5).phase == "WAITING_CI"
    assert effects.calls == {"ci": 1}


def test_connection_is_closed_after_success_and_rollback(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    with store._connection() as successful:
        successful.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        successful.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with store._connection() as failed:
            failed.execute("INSERT INTO schema_version VALUES (99)")
            raise RuntimeError("interrupção")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    assert SqliteExecutionStore(store.database_path).list_history() == ()


@pytest.mark.parametrize("field,value", [
    ("repository_identity", "acme/repo"), ("project_item_id", "item"),
    ("branch", "work/frozen"), ("worktree_path", "C:/worktree"),
    ("base_ref", "origin/main"), ("base_sha", "a" * 40),
    ("pull_request_number", 1), ("pull_request_url", "https://example.invalid/pull/1"),
])
@pytest.mark.parametrize("operation", ["checkpoint", "transition"])
@pytest.mark.parametrize("clear", [False, True])
def test_persisted_identity_cannot_change_or_disappear(tmp_path, field, value, operation, clear):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(1)
    original = store.checkpoint(run.id, summary="Identidade atribuída", **{field: value})
    args = (run.id, ExecutionPhase.CODEX_RUNNING) if operation == "transition" else (run.id,)
    with pytest.raises(ExecutionStoreError, match="Identidade"):
        getattr(store, operation)(*args, summary="Troca", **{field: None if clear else 2 if isinstance(value, int) else "outro"})
    assert store.get(run.id) == original


def test_failed_published_run_blocks_new_claim_until_explicit_supersession(tmp_path):
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(1)
    store.checkpoint(run.id, summary="Publicado", pull_request_number=2)
    failed = store.fail(run.id, "erro não transitório")
    with pytest.raises(ActiveExecutionError, match="reconciliação"):
        store.create(1)
    assert len(store.list_history()) == 1
    store.supersede(run.id, expected=failed, summary="Substituição explícita")
    assert store.create(1).id != run.id


def test_review_read_holds_write_reservation_and_head_change_invalidates_evidence(tmp_path, monkeypatch):
    from ai_dev_orchestrator.infrastructure import database
    from test_recovery_executor import at, review, HEAD, NEW_HEAD
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = at(store, ExecutionPhase.GEMINI_REVIEWING)
    observed, release = Event(), Event()
    original_record = database._record

    def paused_record(row):
        current = original_record(row)
        if not observed.is_set():
            observed.set()
            assert release.wait(5)
        return current

    monkeypatch.setattr(database, "_record", paused_record)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(store.record_review, run.id, review(), "Review atômica")
        try:
            assert observed.wait(5)
            # Esta tentativa não pode escrever no intervalo entre leitura e review.
            with sqlite3.connect(store.database_path, timeout=0) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("UPDATE executions SET current_head_sha = ? WHERE id = ?", (NEW_HEAD, run.id))
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.reviewed_head_sha == HEAD
    assert store.review_findings(run.id, HEAD)
    changed = store.checkpoint(run.id, summary="Novo commit", current_head_sha=NEW_HEAD)
    assert changed.reviewed_head_sha is None and changed.review_verdict is None
    assert changed.ci_head_sha is None and changed.merged_head_sha is None


def test_supersession_does_not_apply_old_proof_to_changed_checkpoint(tmp_path):
    from test_supersession import Observer, stored
    store, run = stored(tmp_path, ExecutionPhase.TESTING)

    class RacingObserver(Observer):
        def observe(self, expected):
            observation = super().observe(expected)
            store.checkpoint(expected.id, summary="Avanço durante consulta", current_head_sha="b" * 40)
            return observation

    with pytest.raises(ExecutionStoreError, match="mudou"):
        SupersessionService(store, RacingObserver()).supersede(run.issue_number, "substituir")
    assert store.get(run.id).phase is ExecutionPhase.TESTING


def test_contract_recovery_does_not_apply_old_proof_to_changed_checkpoint(tmp_path):
    from types import SimpleNamespace
    store = SqliteExecutionStore(tmp_path / "state.db")
    run = store.create(1, base_sha="a" * 40, contract_fingerprint="old")
    service = ContractRecoveryService(None, store, None)
    recovered = SimpleNamespace(fingerprint="confirmed", to_json=lambda: "{}")

    def stale_preview(_issue):
        store.checkpoint(run.id, summary="Mudança concorrente", contract_fingerprint="newer")
        return ContractRecoveryPreview(run, recovered)

    service.preview = stale_preview
    with pytest.raises(ExecutionStoreError, match="mudou"):
        service.recover(1, expected_fingerprint="confirmed")
    assert store.get(run.id).contract_fingerprint == "newer"


@pytest.mark.parametrize("dirty,head,base", [
    (True, "a" * 40, "a" * 40), (False, "b" * 40, "a" * 40),
    (False, "a" * 40, None),
])
def test_preparing_requires_clean_worktree_and_exact_persisted_base(dirty, head, base):
    from test_recovery_planner import run, observed, plan
    checkpoint = run(ExecutionPhase.PREPARING, base_sha=base, current_head_sha=None)
    assert plan(checkpoint, observed(has_worktree_changes=dirty, local_head_sha=head)).action is RecoveryAction.BLOCK


@pytest.mark.parametrize("issues", [(), (99,), (37, 99)])
def test_pr_adoption_requires_exclusive_issue_proof(issues):
    from test_recovery_planner import run, observed, plan, pull_request, HEAD
    checkpoint = run(ExecutionPhase.PR_PENDING)
    snapshot = observed(remote_head_sha=HEAD, pull_requests=(replace(pull_request(), issue_numbers=issues),))
    assert plan(checkpoint, snapshot).action is RecoveryAction.BLOCK
