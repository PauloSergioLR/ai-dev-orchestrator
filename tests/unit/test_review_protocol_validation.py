"""Diagnósticos estritos do reviewer sem processo, provider ou rede reais."""

import json
from pathlib import Path

import pytest

from ai_dev_orchestrator.adapters.antigravity import AntigravityAdapter, AntigravityError
from ai_dev_orchestrator.domain.provider import (
    FAILURE_POLICY, FailureDisposition, ProviderFailure, ProviderFailureKind as Kind,
    sanitized_diagnostic_context, sanitized_protocol_diagnostic,
)
from ai_dev_orchestrator.infrastructure.process import CommandResult
from ai_dev_orchestrator.services.review import (
    REVIEW_PLAN_SCHEMA, STRUCTURED_REVIEW_SCHEMA, ReviewProtocolError,
    parse_review_plan, parse_structured_review,
)


SHA = "a" * 40
SECRET = "token=segredo-nao-persistir"


def valid_review():
    return {"verdict": "APPROVED", "findings": [], "reviewed_head_sha": SHA, "summary": "ok"}


class Runner:
    def __init__(self, output, returncode=0):
        self.output, self.returncode, self.calls = output, returncode, []

    def run(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        if arguments[-1] == "--version":
            return CommandResult(0, "1.1.27")
        if arguments[-1] == "--help":
            return CommandResult(0, (Path(__file__).parents[1] / "fixtures/antigravity/help-1.1.27.txt").read_text(encoding="utf-8"))
        return CommandResult(self.returncode, self.output)


@pytest.mark.parametrize("output,kind,code", [
    ("{" + SECRET, Kind.PROTOCOL_MALFORMED_RESPONSE, "ENVELOPE_INVALID_JSON"),
    ("[]", Kind.PROTOCOL_MALFORMED_RESPONSE, "ENVELOPE_NOT_OBJECT"),
    (json.dumps({"response": SECRET}), Kind.PROTOCOL_MALFORMED_RESPONSE, "ENVELOPE_INVALID_STATUS"),
    (json.dumps({"status": "SUCCESS", "response": SECRET}), Kind.PROTOCOL_MALFORMED_RESPONSE, "SUCCESS_WITHOUT_STRUCTURED_OUTPUT"),
    (json.dumps({"status": "SUCCESS", "structured_output": []}), Kind.PROTOCOL_SCHEMA_MISMATCH, "STRUCTURED_OUTPUT_NOT_OBJECT"),
    (json.dumps({"status": "SUCCESS", "structured_output": SECRET}), Kind.PROTOCOL_SCHEMA_MISMATCH, "STRUCTURED_OUTPUT_NOT_OBJECT"),
    (json.dumps({"status": "SUCCESS", "structured_output": None}), Kind.PROTOCOL_SCHEMA_MISMATCH, "STRUCTURED_OUTPUT_NOT_OBJECT"),
    (json.dumps({"status": "SUCCESS", "structured_output": valid_review(), "error": SECRET}), Kind.PROTOCOL_SEMANTIC_INVALID, "SUCCESS_WITH_ERROR"),
    (json.dumps({"status": "SUCCESS", "structured_output": valid_review(), "denied_actions": [SECRET]}), Kind.PROTOCOL_SEMANTIC_INVALID, "DENIED_ACTIONS"),
    (json.dumps({"status": "SUCCESS", "denied_actions": [SECRET]}), Kind.PROTOCOL_SEMANTIC_INVALID, "DENIED_ACTIONS"),
    (json.dumps({"status": "ERROR"}), Kind.PROTOCOL_SEMANTIC_INVALID, "ENVELOPE_NON_SUCCESS"),
    ('{"status":"SUCCESS","structured_output":{"verdict":"REJECTED","verdict":"APPROVED"}}', Kind.PROTOCOL_MALFORMED_RESPONSE, "JSON_DUPLICATE_KEY"),
    ('{"status":"ERROR","status":"SUCCESS","structured_output":{}}', Kind.PROTOCOL_MALFORMED_RESPONSE, "JSON_DUPLICATE_KEY"),
    ('{"status":"SUCCESS","structured_output":{"summary":NaN}}', Kind.PROTOCOL_MALFORMED_RESPONSE, "JSON_NON_FINITE_NUMBER"),
    (" " * 1_048_577, Kind.PROTOCOL_MALFORMED_RESPONSE, "JSON_OVERSIZED"),
    ("[" * 2_000, Kind.PROTOCOL_MALFORMED_RESPONSE, "ENVELOPE_INVALID_JSON"),
], ids=lambda value: str(value)[:30])
def test_adapter_diferencia_falhas_sem_expor_saida(tmp_path, output, kind, code):
    with pytest.raises(AntigravityError) as caught:
        AntigravityAdapter(1, Runner(output)).invoke("p", tmp_path, STRUCTURED_REVIEW_SCHEMA)
    assert caught.value.classification is kind
    assert caught.value.diagnostic_context == f"protocol={code}"
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.diagnostic_context


@pytest.mark.parametrize("change,kind,code", [
    ({"summary": None}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_TYPE"),
    ({"summary": ""}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_VALUE"),
    ({"verdict": SECRET}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_ENUM"),
    ({"verdict": []}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_TYPE"),
    ({"reviewed_head_sha": "b" * 40}, Kind.PROTOCOL_HEAD_MISMATCH, "HEAD_MISMATCH"),
    ({"reviewed_head_sha": None}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_TYPE"),
    ({SECRET: SECRET}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_EXTRA_FIELDS"),
    ({"findings": [SECRET]}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_TYPE"),
    ({"findings": [{"severity": "HIGH", "title": "t", "description": SECRET}]}, Kind.PROTOCOL_SEMANTIC_INVALID, "APPROVED_WITH_BLOCKING_FINDING"),
    ({"findings": [{"severity": "BAD", "title": "t", "description": SECRET}]}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_ENUM"),
    ({"findings": [{"severity": "LOW", "title": "t", "description": "d", "line": True}]}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_INVALID_TYPE"),
    ({"findings": [{"severity": "LOW", "title": "t", "description": "d", SECRET: SECRET}]}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_EXTRA_FIELDS"),
    ({"findings": [{"severity": "LOW", "title": "t"}]}, Kind.PROTOCOL_SCHEMA_MISMATCH, "SCHEMA_MISSING_FIELDS"),
])
def test_review_distingue_schema_head_e_semantica(change, kind, code):
    payload = {**valid_review(), **change}
    with pytest.raises(ReviewProtocolError) as caught:
        parse_structured_review(json.dumps(payload), SHA, ("HIGH",))
    assert caught.value.classification is kind
    assert caught.value.diagnostic_context == f"protocol={code}"
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.diagnostic_context


@pytest.mark.parametrize("field", STRUCTURED_REVIEW_SCHEMA["required"])
def test_cada_campo_obrigatorio_ausente_tem_diagnostico_fixo(field):
    payload = valid_review()
    del payload[field]
    with pytest.raises(ReviewProtocolError) as caught:
        parse_structured_review(json.dumps(payload), SHA, ("HIGH",))
    assert caught.value.classification is Kind.PROTOCOL_SCHEMA_MISMATCH
    assert caught.value.diagnostic_context == "protocol=SCHEMA_MISSING_FIELDS"


def test_sha_divergente_prevalece_sobre_campo_ausente_ou_extra():
    payload = {"reviewed_head_sha": "b" * 40, SECRET: SECRET}
    with pytest.raises(ReviewProtocolError) as caught:
        parse_structured_review(json.dumps(payload), SHA, ("HIGH",))
    assert caught.value.classification is Kind.PROTOCOL_HEAD_MISMATCH
    assert caught.value.diagnostic_context == "protocol=HEAD_MISMATCH"


@pytest.mark.parametrize("defect", ["missing_summary", "extra_field", "missing_finding_title", "extra_finding_field", "invalid_other_finding"])
def test_approved_bloqueante_prevalece_sobre_defeito_de_schema(defect):
    payload = valid_review()
    payload["findings"] = [{"severity": "HIGH", "title": "t", "description": "d"}]
    if defect == "missing_summary":
        del payload["summary"]
    elif defect == "extra_field":
        payload[SECRET] = SECRET
    elif defect == "missing_finding_title":
        del payload["findings"][0]["title"]
    elif defect == "extra_finding_field":
        payload["findings"][0][SECRET] = SECRET
    else:
        payload["findings"].insert(0, SECRET)
    with pytest.raises(ReviewProtocolError) as caught:
        parse_structured_review(json.dumps(payload), SHA, ("HIGH",))
    assert caught.value.classification is Kind.PROTOCOL_SEMANTIC_INVALID
    assert caught.value.diagnostic_context == "protocol=APPROVED_WITH_BLOCKING_FINDING"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("parse", [
    parse_review_plan,
    lambda output: parse_structured_review(output, SHA, ("HIGH",)),
])
@pytest.mark.parametrize("output,code", [
    ("{" + SECRET, "JSON_INVALID"),
    ('{"x":1,"x":2}', "JSON_DUPLICATE_KEY"),
    ('{"x":Infinity}', "JSON_NON_FINITE_NUMBER"),
])
def test_planner_e_reviewer_rejeitam_json_nao_estrito(parse, output, code):
    with pytest.raises(ReviewProtocolError) as caught:
        parse(output)
    assert caught.value.classification is Kind.PROTOCOL_MALFORMED_RESPONSE
    assert caught.value.diagnostic_context == f"protocol={code}"
    assert SECRET not in str(caught.value)


def test_plano_incompativel_preserva_taxonomia_de_schema():
    plan = {field: [] for field in REVIEW_PLAN_SCHEMA["required"]}
    plan["risks"] = SECRET
    with pytest.raises(ReviewProtocolError) as caught:
        parse_review_plan(json.dumps(plan))
    assert caught.value.classification is Kind.PROTOCOL_SCHEMA_MISMATCH
    assert caught.value.diagnostic_context == "protocol=SCHEMA_INVALID_TYPE"


@pytest.mark.parametrize("exit_code", [0, 2])
@pytest.mark.parametrize("detail", [
    f"unknown option '--json-schema': {SECRET}",
    f"invalid JSON schema: {SECRET}",
    f"unsupported schema: {SECRET}",
])
def test_rejeicao_explicita_de_cli_schema_nao_e_transitoria(tmp_path, exit_code, detail):
    output = json.dumps({"status": "ERROR", "error": detail})
    with pytest.raises(AntigravityError) as caught:
        AntigravityAdapter(1, Runner(output, exit_code)).invoke("p", tmp_path, STRUCTURED_REVIEW_SCHEMA)
    assert caught.value.classification is Kind.PROTOCOL_CLI_INCOMPATIBLE
    assert caught.value.diagnostic_context == "protocol=CLI_SCHEMA_INCOMPATIBLE"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("code,kind", [
    ("QUOTA_EXCEEDED", Kind.TERMINAL_QUOTA),
    ("RATE_LIMIT", Kind.TRANSIENT_RATE_LIMIT),
    ("AUTH_ERROR", Kind.AUTH_ERROR),
    ("NETWORK_ERROR", Kind.NETWORK_ERROR),
    ("MODEL_UNAVAILABLE", Kind.MODEL_UNAVAILABLE),
    ("INVALID_SCHEMA", Kind.PROTOCOL_CLI_INCOMPATIBLE),
])
def test_erro_estruturado_preserva_politica_propria(tmp_path, code, kind):
    output = json.dumps({"status": "ERROR", "error": {"code": code, "message": SECRET}})
    with pytest.raises(ProviderFailure) as caught:
        AntigravityAdapter(1, Runner(output)).invoke("p", tmp_path, STRUCTURED_REVIEW_SCHEMA)
    assert caught.value.classification is kind
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("detail,kind", [
    ("quota exceeded", Kind.TERMINAL_QUOTA),
    ("authentication failed", Kind.AUTH_ERROR),
    ("network error", Kind.NETWORK_ERROR),
    ("model unavailable", Kind.MODEL_UNAVAILABLE),
])
def test_falha_de_processo_nao_recebe_retry_de_protocolo(tmp_path, detail, kind):
    with pytest.raises(ProviderFailure) as caught:
        AntigravityAdapter(1, Runner(detail + "; " + SECRET, 2)).invoke("p", tmp_path, STRUCTURED_REVIEW_SCHEMA)
    assert caught.value.classification is kind


def test_cli_sem_capacidade_tem_diagnostico_sanitizado(tmp_path):
    class OldRunner(Runner):
        def run(self, arguments, **kwargs):
            if arguments[-1] == "--help":
                return CommandResult(0, SECRET)
            return super().run(arguments, **kwargs)

    runner = OldRunner("")
    with pytest.raises(AntigravityError) as caught:
        AntigravityAdapter(1, runner).invoke("p", tmp_path, STRUCTURED_REVIEW_SCHEMA)
    assert caught.value.classification is Kind.PROTOCOL_CLI_INCOMPATIBLE
    assert caught.value.diagnostic_context == "protocol=CLI_CAPABILITY_MISSING"
    assert all("input_text" not in kwargs for _, kwargs in runner.calls)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("kind", [
    Kind.PROTOCOL_MALFORMED_RESPONSE, Kind.PROTOCOL_SCHEMA_MISMATCH,
    Kind.PROTOCOL_HEAD_MISMATCH, Kind.PROTOCOL_CLI_INCOMPATIBLE, Kind.PROTOCOL_SEMANTIC_INVALID,
])
def test_falha_de_protocolo_nunca_usa_retry_generico(kind):
    assert FAILURE_POLICY[kind] is FailureDisposition.INTERVENTION


@pytest.mark.parametrize("value", [
    SECRET, "protocol=" + SECRET, "protocol=SCHEMA_MISSING_FIELDS; " + SECRET,
    "protocol=SCHEMA_MISSING_FIELDS\n" + SECRET, {}, None,
])
def test_diagnostico_recusa_dados_arbitrarios(value):
    assert sanitized_diagnostic_context(value) is None
    assert sanitized_protocol_diagnostic(value) is None
    assert AntigravityError("falha", diagnostic_context=value).diagnostic_context is None


def test_diagnostico_fixo_sobrevive_a_sanitizacao():
    value = sanitized_protocol_diagnostic("SCHEMA_MISSING_FIELDS")
    assert sanitized_diagnostic_context(value) == "protocol=SCHEMA_MISSING_FIELDS"
