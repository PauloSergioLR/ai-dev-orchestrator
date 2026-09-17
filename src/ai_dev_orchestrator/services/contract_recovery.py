"""Recuperação explícita de contrato histórico a partir da base imutável."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from ai_dev_orchestrator.adapters.git import GitWorktreeAdapter
from ai_dev_orchestrator.config import OrchestratorConfig
from ai_dev_orchestrator.domain.execution import RunRecord, TERMINAL_PHASES
from ai_dev_orchestrator.domain.project_contract import ProjectContract
from ai_dev_orchestrator.infrastructure.database import SqliteExecutionStore
from ai_dev_orchestrator.services.pipeline import configured_contract_overrides
from ai_dev_orchestrator.services.project_discovery import ProjectCapabilityResolver


class ContractRecoveryError(Exception):
    """A base ou o contrato reconstruído não fornecem prova segura."""


@dataclass(frozen=True)
class ContractRecoveryPreview:
    run: RunRecord
    recovered: ProjectContract

    @property
    def changed(self) -> bool:
        return self.run.contract_fingerprint != self.recovered.fingerprint


class ContractRecoveryService:
    """Reconstrói sem ler o worktree corrente e só adota após consentimento."""

    def __init__(
        self,
        config: OrchestratorConfig,
        store: SqliteExecutionStore,
        worktrees: GitWorktreeAdapter,
    ) -> None:
        self.config = config
        self.store = store
        self.worktrees = worktrees

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> "ContractRecoveryService":
        return cls(
            config,
            SqliteExecutionStore(config.state.database_path),
            GitWorktreeAdapter(),
        )

    def preview(self, issue_number: int) -> ContractRecoveryPreview:
        run = self.store.get_latest_for_issue(issue_number)
        if run is None:
            raise ContractRecoveryError(
                f"Nenhuma execução encontrada para a Issue #{issue_number}"
            )
        if run.phase in TERMINAL_PHASES:
            raise ContractRecoveryError("Execução terminal não precisa trocar contrato")
        if not run.base_sha:
            raise ContractRecoveryError(
                "Execução histórica não possui base_sha comprovável; contrato preservado"
            )
        if (
            run.repository_identity
            and run.repository_identity != self.config.github.repository_full_name
        ):
            raise ContractRecoveryError("Identidade do repositório diverge da configuração")
        return ContractRecoveryPreview(run, self._resolve_from_base(run.base_sha))

    def recover(
        self, issue_number: int, *, expected_fingerprint: str
    ) -> RunRecord:
        preview = self.preview(issue_number)
        if preview.recovered.fingerprint != expected_fingerprint:
            raise ContractRecoveryError(
                "Contrato reconstruído mudou desde a confirmação; nenhuma adoção feita"
            )
        return self.store.checkpoint(
            preview.run.id,
            summary=(
                "Contrato baseline reconstruído explicitamente a partir de base_sha; "
                "nenhum comando candidato foi executado"
            ),
            contract_fingerprint=preview.recovered.fingerprint,
            project_contract_json=preview.recovered.to_json(),
            candidate_contract_fingerprint=None,
            candidate_contract_json=None,
        )

    def _resolve_from_base(self, base_sha: str) -> ProjectContract:
        root = self.config.workspace.worktrees_dir
        root.mkdir(parents=True, exist_ok=True)
        parent = Path(mkdtemp(prefix=".contract-recovery-", dir=root))
        snapshot = parent / "snapshot"
        created = False
        try:
            self.worktrees.create_detached_worktree(
                self.config.workspace.repository_path, snapshot, base_sha
            )
            created = True
            contract = ProjectCapabilityResolver().resolve(
                snapshot,
                repository_identity=self.config.github.repository_full_name,
                base_branch=self.config.workspace.base_branch,
                pull_request_target=self.config.github.pull_request_base,
                protected_branches=self.config.github.protected_branches,
                overrides=configured_contract_overrides(self.config),
            )
            if contract.ambiguities:
                raise ContractRecoveryError(
                    "Contrato reconstruído continua ambíguo: "
                    + "; ".join(contract.ambiguities)
                )
            return contract
        finally:
            if created:
                self.worktrees.remove_worktree(
                    self.config.workspace.repository_path, snapshot
                )
            if parent.exists():
                parent.rmdir()
