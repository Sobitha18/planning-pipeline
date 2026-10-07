"""LLMGateway tests. No real network calls — the anthropic client's
`.messages.create` is monkeypatched with a fake. Live smoke tests (real API
calls) are marked `live` and excluded by default; run with `pytest -m live`.
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from src import llm as llm_mod
from src.llm import BudgetExceeded, LLMGateway, LLMValidationError, TokenBudget


class Trivial(BaseModel):
    a: str
    b: int


class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=5):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeMessage:
    def __init__(self, text, input_tokens=10, output_tokens=5):
        self.content = [FakeBlock(text)]
        self.usage = FakeUsage(input_tokens, output_tokens)


class FakeCreate:
    """Records every call; returns responses from `replies` in order (or
    raises if the entry is an Exception)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.delenv("LLM_CHEAP_MODEL", raising=False)
    monkeypatch.delenv("LLM_STRONG_MODEL", raising=False)
    gw = LLMGateway(api_key="test-key")
    return gw


def install_fake(monkeypatch, gw, replies):
    fake = FakeCreate(replies)
    monkeypatch.setattr(gw.client.messages, "create", fake)
    return fake


# --- 1. complete_json happy path -------------------------------------------


def test_complete_json_happy_path(gateway, monkeypatch):
    fake = install_fake(monkeypatch, gateway, [FakeMessage('{"a": "hi", "b": 1}')])
    result = gateway.complete_json(tier="cheap", system="sys", user="usr", schema=Trivial)
    assert result == Trivial(a="hi", b=1)
    assert len(fake.calls) == 1


def test_complete_json_writes_debug_transcript_when_run_id_set(gateway, monkeypatch, tmp_path):
    monkeypatch.setenv("DEBUG_LOG_DIR", str(tmp_path))
    install_fake(monkeypatch, gateway, [FakeMessage('{"a": "hi", "b": 1}')])
    result = gateway.complete_json(
        tier="cheap", system="the-system-prompt", user="usr", schema=Trivial, run_id="run-x",
    )
    assert result == Trivial(a="hi", b=1)  # logging changed no output
    text = (tmp_path / "run-run-x.md").read_text()
    assert "the-system-prompt" in text
    assert '"a": "hi"' in text


def test_complete_json_no_transcript_without_run_id(gateway, monkeypatch, tmp_path):
    monkeypatch.setenv("DEBUG_LOG_DIR", str(tmp_path))
    install_fake(monkeypatch, gateway, [FakeMessage('{"a": "hi", "b": 1}')])
    gateway.complete_json(tier="cheap", system="sys", user="usr", schema=Trivial)
    assert list(tmp_path.iterdir()) == []


# --- 2. fenced JSON is stripped and parsed ---------------------------------


def test_complete_json_strips_markdown_fences(gateway, monkeypatch):
    install_fake(monkeypatch, gateway, [FakeMessage('```json\n{"a": "hi", "b": 2}\n```')])
    result = gateway.complete_json(tier="cheap", system="sys", user="usr", schema=Trivial)
    assert result == Trivial(a="hi", b=2)


# --- 3. invalid JSON then valid on retry -----------------------------------


def test_complete_json_retries_on_invalid_then_succeeds(gateway, monkeypatch):
    fake = install_fake(
        monkeypatch,
        gateway,
        [FakeMessage("not json at all"), FakeMessage('{"a": "hi", "b": 3}')],
    )
    result = gateway.complete_json(tier="cheap", system="sys", user="usr", schema=Trivial, retries=2)
    assert result == Trivial(a="hi", b=3)
    assert len(fake.calls) == 2
    retry_prompt = fake.calls[1]["messages"][-1]["content"]
    assert "failed validation" in retry_prompt
    assert "not json at all" == fake.calls[1]["messages"][-2]["content"]


# --- 4. retries exhausted -> LLMValidationError with raw response ----------


def test_complete_json_raises_after_exhausting_retries(gateway, monkeypatch):
    fake = install_fake(
        monkeypatch,
        gateway,
        [FakeMessage("nope"), FakeMessage("still nope")],
    )
    with pytest.raises(LLMValidationError) as excinfo:
        gateway.complete_json(tier="cheap", system="sys", user="usr", schema=Trivial, retries=1)
    assert excinfo.value.raw_response == "still nope"
    assert len(fake.calls) == 2


# --- 5. tier routing --------------------------------------------------------


def test_tier_routing_picks_configured_models(monkeypatch):
    monkeypatch.setenv("LLM_CHEAP_MODEL", "cheap-model-x")
    monkeypatch.setenv("LLM_STRONG_MODEL", "strong-model-y")
    gw = LLMGateway(api_key="test-key")
    fake = install_fake(monkeypatch, gw, [FakeMessage("cheap reply"), FakeMessage("strong reply")])

    gw.complete_text(tier="cheap", system="s", user="u")
    gw.complete_text(tier="strong", system="s", user="u")

    assert fake.calls[0]["model"] == "cheap-model-x"
    assert fake.calls[1]["model"] == "strong-model-y"


def test_unknown_tier_raises(gateway):
    with pytest.raises(ValueError):
        gateway.complete_text(tier="medium", system="s", user="u")


# --- 6. BudgetExceeded raised before the client is called ------------------


def test_budget_exceeded_before_call(gateway, monkeypatch):
    fake = install_fake(monkeypatch, gateway, [FakeMessage("should not be used")])
    budget = TokenBudget(max_tokens=10)  # estimate will blow well past this
    with pytest.raises(BudgetExceeded):
        gateway.complete_text(tier="cheap", system="s", user="u", max_tokens=2048, budget=budget)
    assert fake.calls == []


def test_budget_tracks_usage_across_calls(gateway, monkeypatch):
    install_fake(monkeypatch, gateway, [FakeMessage("ok", input_tokens=3, output_tokens=4)])
    budget = TokenBudget(max_tokens=1_000_000)
    gateway.complete_text(tier="cheap", system="s", user="u", budget=budget)
    assert budget.used == 7


# --- 7. logging row written on success and on failure -----------------------


def test_logging_called_on_success(gateway, monkeypatch):
    logged = []
    monkeypatch.setattr(llm_mod, "_log_call", lambda **kw: logged.append(kw))
    install_fake(monkeypatch, gateway, [FakeMessage("hello", input_tokens=1, output_tokens=2)])

    gateway.complete_text(tier="cheap", system="s", user="u", purpose="greet")

    assert len(logged) == 1
    row = logged[0]
    assert row["ok"] is True
    assert row["purpose"] == "greet"
    assert row["input_tokens"] == 1
    assert row["output_tokens"] == 2
    assert row["error"] is None
    assert isinstance(row["duration_ms"], int)


def test_logging_called_on_transport_failure(gateway, monkeypatch):
    logged = []
    monkeypatch.setattr(llm_mod, "_log_call", lambda **kw: logged.append(kw))
    install_fake(monkeypatch, gateway, [RuntimeError("boom")])

    with pytest.raises(RuntimeError):
        gateway.complete_text(tier="cheap", system="s", user="u")

    assert len(logged) == 1
    assert logged[0]["ok"] is False
    assert "boom" in logged[0]["error"]


def test_logging_called_on_validation_failure(gateway, monkeypatch):
    logged = []
    monkeypatch.setattr(llm_mod, "_log_call", lambda **kw: logged.append(kw))
    install_fake(
        monkeypatch,
        gateway,
        [FakeMessage("bad"), FakeMessage('{"a": "x", "b": 1}')],
    )

    gateway.complete_json(tier="cheap", system="s", user="u", schema=Trivial, retries=1)

    assert len(logged) == 2
    assert logged[0]["ok"] is False
    assert logged[1]["ok"] is True


def test_logging_does_not_fail_call_when_db_unavailable(gateway, monkeypatch, capsys):
    def broken_get_session():
        raise RuntimeError("no db here")

    monkeypatch.setattr("src.db.get_session", broken_get_session)
    install_fake(monkeypatch, gateway, [FakeMessage("hi")])

    text = gateway.complete_text(tier="cheap", system="s", user="u")

    assert text == "hi"
    assert "failed to log call" in capsys.readouterr().err


# --- prompt loader -----------------------------------------------------------


def test_load_prompt_reads_file():
    from src.llm import load_prompt

    content = load_prompt("README")
    assert "Prompts are files" in content


def test_load_prompt_missing_file_raises():
    from src.llm import load_prompt

    with pytest.raises(FileNotFoundError):
        load_prompt("does-not-exist")


# --- DB-backed logging (skipped without TEST_DATABASE_URL) -----------------


def test_log_call_writes_real_row(db_url, monkeypatch, gateway):
    from src.db import get_session
    from src.models import LLMCall

    install_fake(monkeypatch, gateway, [FakeMessage("hi", input_tokens=7, output_tokens=8)])
    gateway.complete_text(tier="cheap", system="s", user="u", purpose="db-smoke")

    session = get_session()
    try:
        rows = session.query(LLMCall).filter_by(purpose="db-smoke").all()
    finally:
        session.close()
    assert len(rows) == 1
    assert rows[0].ok is True
    assert rows[0].input_tokens == 7
    assert rows[0].output_tokens == 8


# --- Live smoke (real API calls; excluded by default) -----------------------


class LiveSchema(BaseModel):
    greeting: str
    count: int


@pytest.mark.live
def test_live_cheap_tier():
    assert os.environ.get("LLM_API_KEY"), "LLM_API_KEY must be set for live smoke"
    gw = LLMGateway()
    result = gw.complete_json(
        tier="cheap",
        system="You are a test fixture. Respond only with the requested JSON.",
        user='Return JSON with greeting="hello" and count=3.',
        schema=LiveSchema,
        purpose="live-smoke-cheap",
    )
    assert isinstance(result, LiveSchema)
    print(f"\n[live smoke] cheap tier model={gw._models['cheap']} result={result}")


@pytest.mark.live
def test_live_strong_tier():
    assert os.environ.get("LLM_API_KEY"), "LLM_API_KEY must be set for live smoke"
    gw = LLMGateway()
    result = gw.complete_json(
        tier="strong",
        system="You are a test fixture. Respond only with the requested JSON.",
        user='Return JSON with greeting="hello" and count=3.',
        schema=LiveSchema,
        purpose="live-smoke-strong",
    )
    assert isinstance(result, LiveSchema)
    print(f"\n[live smoke] strong tier model={gw._models['strong']} result={result}")


def test_api_key_comes_from_llm_api_key_not_anthropic_api_key(monkeypatch):
    """ANTHROPIC_API_KEY must never be what authenticates this client: the same
    process env reaches dev_agent's Claude Code CLI, where that name overrides
    the operator's OAuth login."""
    monkeypatch.setenv("LLM_API_KEY", "from-llm-api-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-anthropic-api-key")
    assert LLMGateway().client.api_key == "from-llm-api-key"
    assert LLMGateway(api_key="explicit").client.api_key == "explicit"


# --- run_tool_loop ---------------------------------------------------------


class FakeToolUse:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class FakeToolMessage:
    def __init__(self, blocks, input_tokens=10, output_tokens=5):
        self.content = blocks
        self.usage = FakeUsage(input_tokens, output_tokens)


TOOLS = [{"name": "echo", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


def test_tool_loop_runs_tools_then_returns_final_text(gateway, monkeypatch):
    fake = install_fake(monkeypatch, gateway, [
        FakeToolMessage([FakeBlock("looking"), FakeToolUse("t1", "echo", {"x": 1})]),
        FakeMessage("done"),
    ])
    seen = []
    result = gateway.run_tool_loop(
        tier="cheap", system="sys", user="usr", tools=TOOLS,
        handler=lambda name, args: seen.append((name, args)) or "echoed",
    )
    assert result.text == "done" and result.turns == 2 and not result.exhausted
    assert seen == [("echo", {"x": 1})]
    # the 2nd request carries the assistant's tool_use and our tool_result
    second = fake.calls[1]["messages"]
    assert second[1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": 1}}
    assert second[2]["content"][0]["tool_use_id"] == "t1"
    assert second[2]["content"][0]["content"] == "echoed"
    assert second[2]["content"][0]["is_error"] is False


def test_tool_loop_handler_error_goes_back_to_the_model(gateway, monkeypatch):
    fake = install_fake(monkeypatch, gateway, [
        FakeToolMessage([FakeToolUse("t1", "echo", {})]),
        FakeMessage("recovered"),
    ])

    def boom(name, args):
        raise ValueError("bad arg")

    result = gateway.run_tool_loop(tier="cheap", system="s", user="u", tools=TOOLS, handler=boom)
    assert result.text == "recovered"
    block = fake.calls[1]["messages"][2]["content"][0]
    assert block["is_error"] is True and "bad arg" in block["content"]
    assert result.tool_calls[0]["error"] == "bad arg"


def test_tool_loop_max_turns_forces_an_answer_with_tools_off(gateway, monkeypatch):
    fake = install_fake(monkeypatch, gateway, [
        FakeToolMessage([FakeToolUse("t1", "echo", {})]),
        FakeToolMessage([FakeToolUse("t2", "echo", {})]),   # would be a 3rd tool call...
        FakeMessage("forced answer"),                         # ...but tools are off now
    ])
    result = gateway.run_tool_loop(
        tier="cheap", system="s", user="u", tools=TOOLS, handler=lambda n, a: "ok", max_turns=2)
    assert result.text == "forced answer" and result.exhausted is True
    assert "tool_choice" not in fake.calls[0] and "tool_choice" not in fake.calls[1]
    assert fake.calls[2]["tool_choice"] == {"type": "none"}
    assert "Tool budget exhausted" in fake.calls[2]["messages"][-1]["content"]


def test_tool_loop_respects_token_budget(gateway, monkeypatch):
    install_fake(monkeypatch, gateway, [FakeMessage("never reached")])
    with pytest.raises(BudgetExceeded):
        gateway.run_tool_loop(tier="cheap", system="s", user="u", tools=TOOLS,
                              handler=lambda n, a: "", budget=TokenBudget(max_tokens=1))
