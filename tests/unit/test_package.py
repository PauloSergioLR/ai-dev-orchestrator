"""Testes do pacote."""

import ai_dev_orchestrator


def test_package_can_be_imported() -> None:
    assert ai_dev_orchestrator.__version__ == "0.1.0"


def test_politica_de_review_e_recurso_do_pacote(monkeypatch, tmp_path) -> None:
    from ai_dev_orchestrator.services.review import load_review_policy

    monkeypatch.chdir(tmp_path)
    assert "Atue somente como reviewer técnico independente" in load_review_policy()
