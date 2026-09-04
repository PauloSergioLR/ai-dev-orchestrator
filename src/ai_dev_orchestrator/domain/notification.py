"""Evento operacional seguro e porta desacoplada dos canais de entrega."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Protocol


@dataclass(frozen=True)
class HumanRequiredNotification:
    execution_id: str
    repository: str
    issue_number: int
    reason_code: str
    reason: str
    blocked_phase: str
    occurred_at: datetime
    branch: str | None = None
    worktree_path: str | None = None
    codex_session_id: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    head_sha: str | None = None
    ci_head_sha: str | None = None
    review_verdict: str | None = None
    correction_attempts: int = 0
    failure_classification: str | None = None
    suggested_action: str | None = None

    @property
    def event_key(self) -> str:
        # O fingerprint muda apenas quando o estado operacional muda materialmente.
        material = (
            self.reason_code,
            self.reason,
            self.blocked_phase,
            self.pull_request_number,
            self.head_sha,
            self.correction_attempts,
            self.failure_classification,
        )
        encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
        return f"{self.reason_code}:{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"

    def message(self) -> str:
        lines = [
            f"{self.repository} — Issue #{self.issue_number}",
            "Estado: HUMAN_REQUIRED",
            f"Motivo: {self.reason}",
            f"Fase: {self.blocked_phase}",
        ]
        if self.pull_request_url:
            lines.append(f"PR: {self.pull_request_url}")
        elif self.pull_request_number:
            lines.append(f"PR: #{self.pull_request_number}")
        if self.head_sha:
            lines.append(f"HEAD: {self.head_sha}")
        lines.append(f"Correções: {self.correction_attempts}")
        if self.suggested_action:
            lines.append(f"Ação sugerida: {self.suggested_action}")
        lines.append(f"Horário: {self.occurred_at.isoformat()}")
        return "\n".join(lines)


class NotificationChannel(Protocol):
    name: str

    def send(self, event: HumanRequiredNotification) -> None: ...
