"""Prova read-only da identidade local da sessão, sem ler prompts ou chamar a CLI."""

import json
import os
from pathlib import Path
import re


def session_matches_worktree(session_id: str | None, worktree: str | None,
                             codex_home: Path | None = None) -> bool:
    if not session_id or not worktree or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return False
    root = codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        matches = list((root / "sessions").rglob(f"rollout-*-{session_id}*.jsonl"))
        if len(matches) != 1 or not matches[0].name.endswith(f"-{session_id}.jsonl"):
            return False
        with matches[0].open("rb") as stream:
            line = stream.readline(65537)
        if len(line) > 65536:
            return False
        event = json.loads(line.decode("utf-8", errors="strict"))
        payload = event.get("payload")
        return (event.get("type") == "session_meta" and isinstance(payload, dict)
                and payload.get("id") == session_id and isinstance(payload.get("cwd"), str)
                and Path(payload["cwd"]).resolve() == Path(worktree).resolve())
    except (OSError, ValueError, AttributeError):
        return False
