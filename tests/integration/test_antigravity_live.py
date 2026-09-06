"""Probe opt-in: consome uma chamada real, sem executar o pipeline ou acessar SQLite."""

import json
import os
from pathlib import Path
import tempfile

import pytest

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter
from ai_dev_orchestrator.services.review import (
    REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA, parse_review_plan, parse_structured_review,
)


@pytest.mark.skipif(os.environ.get("ORCH_TEST_ANTIGRAVITY_LIVE") != "1", reason="CLI real opt-in")
def test_installed_cli_returns_both_review_schemas():
    adapter = AntigravityAdapter(60, executable=os.environ.get("ORCH_REVIEW__EXECUTABLE", "agy"))
    # O daemon da CLI pode manter o cwd aberto no Windows após encerrar a chamada.
    with tempfile.TemporaryDirectory(prefix="orch-live-", ignore_cleanup_errors=True) as folder:
        cwd = Path(folder)
        plan = adapter.invoke(
            "Teste de transporte. Retorne todas as listas vazias. Não leia arquivos nem execute comandos.",
            cwd, REVIEW_PLAN_SCHEMA,
        )
        assert parse_review_plan(plan).risks == ()
        sha = "a" * 40
        review = adapter.invoke(
            "Teste de transporte, não é revisão real. Retorne REJECTED, findings vazio, "
            f"summary igual a olá e reviewed_head_sha igual a {sha}. "
            "Não leia arquivos nem execute comandos.",
            cwd, STRUCTURED_REVIEW_SCHEMA,
        )
        assert parse_structured_review(review, sha, ("HIGH",)).verdict == "REJECTED"
        assert json.loads(review)["summary"] == "olá"
