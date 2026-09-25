"""Identidade local do Codex CLI e leitura limitada da configuração global."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tomllib


@dataclass(frozen=True)
class CodexCandidate:
    path: str
    origin: str


def codex_candidates(path: str | None = None) -> tuple[CodexCandidate, ...]:
    """Lista executáveis chamados codex na ordem efetiva do PATH."""
    search_path = os.environ.get("PATH", "") if path is None else path
    extensions = ("",)
    if os.name == "nt":
        extensions = tuple(dict.fromkeys(("", *os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"))))
    candidates: list[CodexCandidate] = []
    seen: set[str] = set()
    for directory in search_path.split(os.pathsep):
        if not directory:
            continue
        for extension in extensions:
            target = Path(directory) / f"codex{extension}"
            if not target.is_file():
                continue
            absolute = str(target.resolve())
            key = os.path.normcase(absolute)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(CodexCandidate(absolute, classify_origin(absolute)))
    selected = shutil.which("codex", path=search_path)
    if selected:
        selected_key = os.path.normcase(str(Path(selected).resolve()))
        candidates.sort(key=lambda candidate: os.path.normcase(candidate.path) != selected_key)
    return tuple(candidates)


def classify_origin(path: str) -> str:
    """Descreve padrões conhecidos sem inferir além do caminho observado."""
    normalized = path.replace("\\", "/").casefold()
    if "/.volta/bin/" in normalized or "/volta/bin/" in normalized:
        return "shim Volta"
    if "/node_modules/" in normalized or "/npm/" in normalized or "/npm/" in normalized:
        return "instalação/shim npm"
    if ("/programs/codex/" in normalized or "/codex/bin/" in normalized
            or "/.codex/bin/" in normalized or "/.local/bin/codex" in normalized
            or "/windowsapps/" in normalized and "codex" in normalized):
        return "instalador oficial (padrão de caminho)"
    return "PATH"


def effective_executable() -> str | None:
    """Retorna a mesma primeira resolução de PATH usada por CommandRunner."""
    return shutil.which("codex")


def global_codex_settings() -> tuple[str | None, str | None, str | None]:
    """Lê exclusivamente model e model_reasoning_effort de config.toml."""
    root = os.environ.get("CODEX_HOME")
    config_path = (Path(root).expanduser() if root else Path.home() / ".codex") / "config.toml"
    try:
        with config_path.open("rb") as config_file:
            payload = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return None, None, None
    model, effort = payload.get("model"), payload.get("model_reasoning_effort")
    return (
        model if isinstance(model, str) else None,
        effort if isinstance(effort, str) else None,
        str(config_path),
    )
