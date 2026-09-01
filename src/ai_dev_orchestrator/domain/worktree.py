"""Modelo interno para uma execução isolada em Git worktree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitWorktree:
    """Representa o worktree e a branch criados para uma execução."""

    repository_root: Path
    path: Path
    branch: str
    base_ref: str
