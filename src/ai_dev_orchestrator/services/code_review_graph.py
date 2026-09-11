"""Integração fail-open com o executável externo Code Review Graph."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Protocol, Sequence

from ai_dev_orchestrator.config import CodeReviewGraphConfig
from ai_dev_orchestrator.infrastructure.database import sanitize_diagnostic_text
from ai_dev_orchestrator.infrastructure.process import CommandResult, CommandRunner


logger = logging.getLogger(__name__)

GRAPH_INSTRUCTION = (
    "Use Code Review Graph para descoberta estrutural do código e análise de impacto "
    "antes de buscas amplas no filesystem. Confirme no código-fonte antes de alterar. "
    "Quando o grafo estiver incompleto, desatualizado, ambíguo, indisponível ou não "
    "suportar a análise, faça fallback para grep e leitura direta de arquivos."
)


class ProcessRunner(Protocol):
    def run(
        self, arguments: Sequence[str], cwd: str | Path | None = None,
        input_text: str | None = None, **kwargs: Any,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class GraphPreparation:
    """Observação pequena e segura de uma tentativa de preparar o grafo."""

    used: bool
    action: str
    duration_seconds: float
    nodes: int | None = None
    edges: int | None = None
    estimated_context_savings: str | None = None
    warning: str | None = None


class CodeReviewGraphIntegrator:
    """Constrói ou atualiza o grafo sem tornar sua falha crítica."""

    def __init__(
        self,
        config: CodeReviewGraphConfig,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner(timeout=config.timeout_seconds)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def version(self) -> tuple[str | None, str | None]:
        """Retorna versão observada e erro sanitizado, sem lançar exceção."""
        result = self.runner.run([*self.config.command, "--version"])
        if not result.succeeded:
            return None, self._failure(result)
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", result.stdout)
        if match is None:
            return None, "saída de versão não reconhecida"
        return match.group(1), None

    def status(self, repository: Path) -> tuple[dict[str, Any] | None, str | None]:
        result = self.runner.run(
            [*self.config.command, "status", "--json", "--repo", str(repository)],
            cwd=repository,
        )
        if not result.succeeded:
            return None, self._failure(result)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, "status retornou JSON inválido"
        if not isinstance(data, dict) or any(
            isinstance(data.get(key), bool) or not isinstance(data.get(key), int)
            or data[key] < 0
            for key in ("nodes", "edges", "files")
        ):
            return None, "status retornou estatísticas inválidas"
        return data, None

    def prepare(self, repository: str | Path) -> GraphPreparation:
        """Garante build inicial ou update incremental, sempre em fail-open."""
        if not self.enabled:
            return GraphPreparation(False, "disabled", 0)
        started = time.monotonic()
        root = Path(repository).resolve()
        try:
            observed_version, version_error = self.version()
            if version_error or observed_version != self.config.required_version:
                detail = version_error or (
                    f"versão {observed_version} incompatível; esperada "
                    f"{self.config.required_version}"
                )
                return self._fallback(started, detail)

            current, _ = self.status(root)
            action = "update" if current is not None else "build"
            arguments = [*self.config.command, action]
            if action == "update":
                arguments.append("--brief")
            arguments.extend(("--repo", str(root)))
            result = self.runner.run(arguments, cwd=root)
            if not result.succeeded:
                return self._fallback(started, f"{action} falhou: {self._failure(result)}")

            stats, status_error = self.status(root)
            if stats is None:
                return self._fallback(
                    started, f"grafo não pôde ser validado após {action}: {status_error}"
                )
            savings = self._context_savings(result.stdout)
            duration = time.monotonic() - started
            logger.info(
                "CRG usado: %s em %.3fs; nós=%s; arestas=%s; economia=%s",
                action, duration, stats["nodes"], stats["edges"], savings or "indisponível",
            )
            logger.debug("CRG status: arquivos=%s", stats["files"])
            return GraphPreparation(
                True, action, duration, stats["nodes"], stats["edges"], savings
            )
        except Exception as error:  # integração externa nunca derruba o pipeline
            return self._fallback(started, str(error))

    def codex_mcp_overrides(self, repository: str | Path) -> tuple[str, ...]:
        """Produz overrides TOML por execução para o MCP usar o worktree correto."""
        if not self.enabled:
            return ()
        command, *prefix = self.config.command
        args = [*prefix, "serve"]
        root = str(Path(repository).resolve())
        return (
            f"mcp_servers.code-review-graph.command={json.dumps(command)}",
            f"mcp_servers.code-review-graph.args={json.dumps(args)}",
            f"mcp_servers.code-review-graph.cwd={json.dumps(root)}",
            "mcp_servers.code-review-graph.required=false",
        )

    def ensure_antigravity_mcp(self) -> bool:
        """Mescla atomicamente o servidor MCP global exigido pelo Antigravity."""
        if not self.enabled:
            return False
        path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
        temporary: Path | None = None
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("a raiz da configuração MCP não é um objeto")
            else:
                data = {}
            servers = data.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("mcpServers não é um objeto")
            command, *prefix = self.config.command
            expected = {"command": command, "args": [*prefix, "serve"]}
            if servers.get("code-review-graph") == expected:
                return True
            servers["code-review-graph"] = expected
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False,
                prefix=".mcp_config.", suffix=".tmp",
            ) as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                temporary = Path(stream.name)
            os.replace(temporary, path)
            temporary = None
            logger.info("MCP CRG do Antigravity configurado")
            return True
        except Exception as error:
            self._fallback(time.monotonic(), f"configuração MCP Antigravity falhou: {error}")
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _fallback(self, started: float, detail: str) -> GraphPreparation:
        safe = (sanitize_diagnostic_text(detail) or "erro sem diagnóstico")[:500]
        duration = time.monotonic() - started
        message = f"Code Review Graph indisponível; usando busca/leitura direta: {safe}"
        logger.warning(message)
        return GraphPreparation(False, "fallback", duration, warning=message)

    @staticmethod
    def _context_savings(output: str) -> str | None:
        fallback = None
        for line in output.splitlines():
            normalized = line.casefold()
            cleaned = (sanitize_diagnostic_text(line.strip(" │┌┐└┘─")) or "")[:200]
            if "saved:" in normalized or "economizado:" in normalized:
                return cleaned or None
            if fallback is None and ("saving" in normalized or "econom" in normalized):
                fallback = cleaned or None
        return fallback

    @staticmethod
    def _failure(result: CommandResult) -> str:
        detail = result.error or result.stderr.strip() or result.stdout.strip()
        if detail:
            return (sanitize_diagnostic_text(detail) or "falha sem diagnóstico")[:500]
        return f"comando retornou código {result.returncode}"
