"""Stage B generation tests, both gates. No network: either a fake gateway
object, or the real LLMGateway with `.messages.create` monkeypatched (for the
schema-strictness cases, which must exercise the gateway's own retry/raise
path)."""

from __future__ import annotations

import json

import pytest

from src import llm as llm_mod
from src.llm import LLMGateway, LLMValidationError
from src.models import ContextPack, PackedFile, Plan, SpecOutput, SpecPlanOutput
from src.planning import (
    _assemble,
    generate_plan,
    generate_spec,
    render_plan_prompt,
    render_user_prompt,
)

# ------------------------------------------------------------------ fixtures

TIER1_BODY = "def build_reset_link(token):\n    return PUBLIC_URL + '/reset/' + token\n"


@pytest.fixture
def pack():
    return ContextPack(
        repo_id=1, sha="deadbeef", request_type="bug",
        files=[
            PackedFile(path="src/auth/reset.py", tier=1, label="critical",
                       reason="builds the emailed link", content=TIER1_BODY),
            PackedFile(path="src/settings.py", tier=2, label="relevant",
                       reason="holds the URL settings", content="PUBLIC_URL: str\nAPP_URL: str"),
            PackedFile(path="tests/test_auth.py", tier=3, label="peripheral",
                       reason="existing auth tests", content="existing auth tests"),
        ],
    )


REQUEST = "Password reset links 404 — they point at the marketing host."


def valid_spec_payload() -> dict:
    return {
        "spec": {
            "problem_statement": "build_reset_link uses PUBLIC_URL instead of APP_URL.",
            "criteria": [{"id": "AC1", "given": "APP_URL is set", "when": "build_reset_link runs",
                          "then": "the URL uses APP_URL", "verify_by": "unit"}],
            "touchable_files": ["src/auth/reset.py", "tests/test_auth.py"],
            "non_goals": ["Will not modify src/settings.py because both settings already exist"],
            "assumptions": [{"text": "APP_URL is set everywhere", "confidence": "high"}],
        },
        "open_questions": [{"q": "Rotate existing tokens?", "default": "No, keep them valid"}],
    }


def valid_plan_payload() -> dict:
    return {
        "tasks": [
            {"id": "t1", "title": "Use APP_URL", "description": "Swap the setting.",
             "files": ["src/auth/reset.py"], "done_criteria": "returns APP_URL-based link",
             "est_size": "S", "criterion_refs": ["AC1"]},
            {"id": "t2", "title": "Test it", "description": "Add unit tests.",
             "files": ["tests/test_auth.py"], "done_criteria": "tests pass",
             "est_size": "S", "criterion_refs": ["AC1"]},
        ],
        "edges": [{"from_task": "t1", "to_task": "t2", "kind": "code"}],
    }


@pytest.fixture
def spec():
    return SpecOutput.model_validate(valid_spec_payload())


class FakeGateway:
    """Returns a canned payload parsed against whatever schema it is handed;
    records the call kwargs."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, *, schema, **kwargs):
        self.calls.append(kwargs)
        return schema.model_validate(self.payload)


def real_gateway(monkeypatch, raw_text: str) -> LLMGateway:
    """Real gateway, fake transport that always returns `raw_text`."""

    class Usage:
        input_tokens, output_tokens = 10, 5

    class Block:
        type, text = "text", raw_text

    class Message:
        content, usage = [Block()], Usage()

    gw = LLMGateway(api_key="test-key")
    monkeypatch.setattr(gw.client.messages, "create", lambda **kw: Message())
    monkeypatch.setattr(llm_mod, "_log_call", lambda **kw: None)
    return gw


# ------------------------------------------------------- 1. prompt rendering


def test_prompt_contains_request_and_every_pack_path(pack):
    prompt = render_user_prompt(REQUEST, pack)
    assert REQUEST in prompt
    for f in pack.files:
        assert f.path in prompt


def test_tier1_content_verbatim_tier3_path_and_reason_only(pack):
    prompt = render_user_prompt(REQUEST, pack)
    assert TIER1_BODY in prompt                       # tier 1: full body
    assert "PUBLIC_URL: str\nAPP_URL: str" in prompt  # tier 2: signatures
    # tier 3: header line only, no fenced body for it
    tier3 = prompt.split("### tests/test_auth.py")[1]
    assert "existing auth tests" in tier3
    assert "```" not in tier3


def test_plan_prompt_carries_the_pack_and_the_approved_spec(pack, spec):
    prompt = render_plan_prompt(REQUEST, pack, spec)
    assert render_user_prompt(REQUEST, pack) in prompt       # same pack rendering
    assert "APPROVED SPEC" in prompt
    assert "do not alter it" in prompt
    assert "src/auth/reset.py" in prompt.split("APPROVED SPEC")[1]
    assert "Rotate existing tokens?" in prompt               # open questions travel too


# ------------------------------------------------------------- 2. happy path


def test_generate_spec_roundtrip(pack):
    gw = FakeGateway(valid_spec_payload())
    out = generate_spec(REQUEST, pack, gw)

    assert isinstance(out, SpecOutput)
    assert out.model_dump() == SpecOutput.model_validate(valid_spec_payload()).model_dump()
    assert out.spec.criteria[0].verify_by == "unit"

    call = gw.calls[0]
    assert call["tier"] == "strong"
    assert call["purpose"] == "spec"
    assert call["max_tokens"] == 4096
    assert "Given/When/Then" in call["system"]


def test_generate_plan_roundtrip(pack, spec):
    gw = FakeGateway(valid_plan_payload())
    plan = generate_plan(REQUEST, pack, spec, gw)

    assert isinstance(plan, Plan)
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert plan.edges[0].from_task == "t1"

    call = gw.calls[0]
    assert call["tier"] == "strong"
    assert call["purpose"] == "plan"
    assert call["max_tokens"] == 8192
    assert "touchable_files" in call["system"]       # the approved-scope rule
    assert "APPROVED SPEC" in call["user"]


# ------------------------------------------------------------- 3. assembly


def test_assemble_copies_the_approved_spec_verbatim(spec):
    """No dual write: the plan half varies, the spec half is byte-identical
    every time — gate 2 never rewrites what gate 1 approved."""
    other_plan = valid_plan_payload()
    other_plan["tasks"] = other_plan["tasks"][:1]
    other_plan["edges"] = []

    first = _assemble(spec, Plan.model_validate(valid_plan_payload()))
    second = _assemble(spec, Plan.model_validate(other_plan))

    assert isinstance(first, SpecPlanOutput)
    assert first.spec.model_dump_json() == spec.spec.model_dump_json()
    assert second.spec.model_dump_json() == first.spec.model_dump_json()
    assert (second.model_dump_json(include={"open_questions"})
            == first.model_dump_json(include={"open_questions"})
            == spec.model_dump_json(include={"open_questions"}))
    assert [t.id for t in second.plan.tasks] != [t.id for t in first.plan.tasks]


# --------------------------------------------------------- 4. schema strictness


def _mutate(base, fn):
    payload = base()
    fn(payload)
    return payload


BAD_SPEC_PAYLOADS = {
    "criterion missing verify_by": _mutate(
        valid_spec_payload, lambda p: p["spec"]["criteria"][0].pop("verify_by")),
    "six open questions": _mutate(
        valid_spec_payload,
        lambda p: p.update(open_questions=[{"q": f"q{i}", "default": "d"} for i in range(6)])),
    "nine criteria": _mutate(
        valid_spec_payload,
        lambda p: p["spec"].update(criteria=[dict(p["spec"]["criteria"][0], id=f"AC{i}")
                                             for i in range(9)])),
}

BAD_PLAN_PAYLOADS = {
    "invalid est_size": _mutate(valid_plan_payload, lambda p: p["tasks"][0].update(est_size="XL")),
    "no tasks": _mutate(valid_plan_payload, lambda p: p.update(tasks=[])),
}


@pytest.mark.parametrize("name", list(BAD_SPEC_PAYLOADS))
def test_bad_spec_payload_exhausts_retries(monkeypatch, pack, name):
    gw = real_gateway(monkeypatch, json.dumps(BAD_SPEC_PAYLOADS[name]))
    with pytest.raises(LLMValidationError):
        generate_spec(REQUEST, pack, gw)


@pytest.mark.parametrize("name", list(BAD_PLAN_PAYLOADS))
def test_bad_plan_payload_exhausts_retries(monkeypatch, pack, spec, name):
    gw = real_gateway(monkeypatch, json.dumps(BAD_PLAN_PAYLOADS[name]))
    with pytest.raises(LLMValidationError):
        generate_plan(REQUEST, pack, spec, gw)


def test_valid_payloads_parse_through_real_gateway(monkeypatch, pack, spec):
    gw = real_gateway(monkeypatch, json.dumps(valid_spec_payload()))
    assert generate_spec(REQUEST, pack, gw).spec.criteria[0].id == "AC1"

    gw = real_gateway(monkeypatch, json.dumps(valid_plan_payload()))
    assert [t.id for t in generate_plan(REQUEST, pack, spec, gw).tasks] == ["t1", "t2"]


# ------------------------------------------------------- 5. deterministic render


def test_same_pack_renders_identically(pack):
    assert render_user_prompt(REQUEST, pack) == render_user_prompt(REQUEST, pack.model_copy(deep=True))
