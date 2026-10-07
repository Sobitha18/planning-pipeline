"""LLM gateway — the single choke point ALL Anthropic API calls flow through.

Model routing (config-driven, never hardcoded at call sites), structured-output
enforcement with a bounded repair-retry loop, transport retry (delegated to
the anthropic SDK's own backoff), per-call logging to `llm_calls`, and an
optional per-run token budget.

Out of scope (see task-prompts/04-llm-gateway.md): streaming, MCP
passthrough, caching, multi-provider abstraction, async client. Tool use is
supported only through `run_tool_loop` (added for the multi-repo router).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

from src.config import get_settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Read src/prompts/<name>.md. Prompts are files, not string literals."""
    return (PROMPTS_DIR / f"{name}.md").read_text()


class LLMValidationError(Exception):
    """complete_json exhausted its retries without producing schema-valid
    JSON. Carries the last raw model response for debugging."""

    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


class BudgetExceeded(Exception):
    """A call would exceed the run's token budget. Raised before the call
    is made — no network request happens."""


@dataclass
class TokenBudget:
    """Cumulative token tracker for one run.

    ponytail: naive running total (estimate pre-call, real usage post-call),
    no per-tier breakdown — add if a run needs spend-by-call-type visibility.
    """

    max_tokens: int
    used: int = 0

    def check(self, estimate: int) -> None:
        if self.used + estimate > self.max_tokens:
            raise BudgetExceeded(
                f"budget exceeded: used={self.used} + estimate={estimate} > max={self.max_tokens}"
            )

    def add(self, tokens: int) -> None:
        self.used += tokens


def _estimate_tokens(prompt: str, max_tokens: int) -> int:
    return int(len(prompt) / 3.5) + max_tokens


def _strip_fences(text: str) -> str:
    """Strip a leading/trailing ```` ```json ... ``` ```` fence if present."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _log_call(
    *,
    tier: str,
    model: str,
    purpose: str,
    input_tokens: int | None,
    output_tokens: int | None,
    duration_ms: int,
    ok: bool,
    error: str | None,
    run_id: int | None,
) -> None:
    """Best-effort call log. Never raises — logging must not fail a call."""
    try:
        from src.db import get_session
        from src.models import LLMCall

        session = get_session()
        try:
            session.add(
                LLMCall(
                    ts=dt.datetime.now(dt.timezone.utc),
                    tier=tier,
                    model=model,
                    purpose=purpose,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    ok=ok,
                    error=error,
                    run_id=run_id,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:  # DB unavailable, schema missing, etc.
        print(f"[llm] failed to log call: {exc}", file=sys.stderr)


@dataclass
class ToolLoopResult:
    text: str                      # the model's final plain-text answer
    turns: int                     # model calls made
    tool_calls: list[dict] = field(default_factory=list)   # [{name, input, error}]
    exhausted: bool = False        # True if max_turns forced the final answer


def _block_to_param(block) -> dict:
    """Response content block -> the dict shape messages.create accepts back."""
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": "text", "text": getattr(block, "text", "")}


class LLMGateway:
    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        # Key passed explicitly from LLM_API_KEY rather than left to the
        # anthropic SDK's ANTHROPIC_API_KEY env fallback — see config.Settings
        # for why that name is poison in this repo. max_retries=3 covers
        # requirement #3 (backoff+jitter on rate-limit/overloaded/5xx) via the
        # SDK's own retry logic — no need to hand-roll it.
        self.client = anthropic.Anthropic(api_key=api_key or settings.llm_api_key, max_retries=3)
        self._models = {"cheap": settings.llm_cheap_model, "strong": settings.llm_strong_model}

    def _model_for(self, tier: str) -> str:
        try:
            return self._models[tier]
        except KeyError:
            raise ValueError(f"unknown tier {tier!r}; expected 'cheap' or 'strong'") from None

    def complete_json(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int = 4096,
        retries: int = 2,
        purpose: str = "unspecified",
        run_id: int | None = None,
        budget: TokenBudget | None = None,
    ) -> BaseModel:
        model = self._model_for(tier)
        schema_json = json.dumps(schema.model_json_schema())
        full_system = (
            f"{system}\n\n"
            "Respond with ONLY JSON matching this schema. No markdown fences, "
            f"no commentary, no text before or after the JSON:\n{schema_json}"
        )
        messages: list[dict] = [{"role": "user", "content": user}]

        last_raw = ""
        attempt = 0
        while True:
            prompt_text = full_system + "".join(m["content"] for m in messages)
            estimate = _estimate_tokens(prompt_text, max_tokens)
            if budget is not None:
                budget.check(estimate)

            start = time.monotonic()
            try:
                response = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=full_system,
                    # Thinking is ON by default on Sonnet-5-class models, and
                    # max_tokens caps thinking + text TOGETHER. A 25k-token
                    # context pack made the model spend all 8192 tokens
                    # thinking and emit no text at all -> "empty response",
                    # 3 retries, ~280s wasted. Disabling it makes max_tokens
                    # mean what every call site already assumes: the JSON
                    # budget.
                    # ponytail: blanket disable at the choke point. If a call
                    # ever needs reasoning, give complete_json a `thinking`
                    # arg and budget max_tokens for both halves.
                    thinking={"type": "disabled"},
                    messages=messages,
                )
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                _log_call(
                    tier=tier, model=model, purpose=purpose, input_tokens=None,
                    output_tokens=None, duration_ms=duration_ms, ok=False,
                    error=str(exc), run_id=run_id,
                )
                raise
            duration_ms = int((time.monotonic() - start) * 1000)
            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            if budget is not None:
                budget.add(in_tok + out_tok)

            raw_text = "".join(b.text for b in response.content if b.type == "text")
            last_raw = raw_text
            stripped = _strip_fences(raw_text)
            try:
                data = json.loads(stripped)
                instance = schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                _log_call(
                    tier=tier, model=model, purpose=purpose, input_tokens=in_tok,
                    output_tokens=out_tok, duration_ms=duration_ms, ok=False,
                    error=str(exc), run_id=run_id,
                )
                attempt += 1
                if attempt > retries:
                    raise LLMValidationError(
                        f"complete_json: exhausted {retries} retries; last error: {exc}",
                        raw_response=last_raw,
                    ) from exc
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": f"Your previous response failed validation: {exc}. "
                               "Respond with corrected JSON only.",
                })
                continue

            _log_call(
                tier=tier, model=model, purpose=purpose, input_tokens=in_tok,
                output_tokens=out_tok, duration_ms=duration_ms, ok=True,
                error=None, run_id=run_id,
            )
            return instance

    def complete_text(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        purpose: str = "unspecified",
        run_id: int | None = None,
        budget: TokenBudget | None = None,
    ) -> str:
        model = self._model_for(tier)
        estimate = _estimate_tokens(system + user, max_tokens)
        if budget is not None:
            budget.check(estimate)

        start = time.monotonic()
        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            _log_call(
                tier=tier, model=model, purpose=purpose, input_tokens=None,
                output_tokens=None, duration_ms=duration_ms, ok=False,
                error=str(exc), run_id=run_id,
            )
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        if budget is not None:
            budget.add(in_tok + out_tok)

        text = "".join(b.text for b in response.content if b.type == "text")
        _log_call(
            tier=tier, model=model, purpose=purpose, input_tokens=in_tok,
            output_tokens=out_tok, duration_ms=duration_ms, ok=True,
            error=None, run_id=run_id,
        )
        return text

    def run_tool_loop(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        tools: list[dict],
        handler: Callable[[str, dict], str],
        max_turns: int = 12,
        max_tokens: int = 2048,
        purpose: str = "unspecified",
        run_id: int | None = None,
        budget: TokenBudget | None = None,
    ) -> ToolLoopResult:
        """Let the model call `tools` until it answers in plain text.

        `handler(name, input) -> str` runs one tool; an exception from it is
        returned to the model as an error result instead of aborting the loop
        (a bad argument is something the model can correct). After `max_turns`
        model calls the tools are switched off (tool_choice none) and the
        model must answer with what it has.
        """
        model = self._model_for(tier)
        messages: list[dict] = [{"role": "user", "content": user}]
        calls: list[dict] = []
        turns = 0
        exhausted = False

        while True:
            force_answer = turns >= max_turns
            if force_answer:
                exhausted = True
                messages.append({
                    "role": "user",
                    "content": "Tool budget exhausted. Give your final answer now, "
                               "using only what you have already found.",
                })
            kwargs = {"tool_choice": {"type": "none"}} if force_answer else {}

            estimate = _estimate_tokens(system + json.dumps(messages, default=str), max_tokens)
            if budget is not None:
                budget.check(estimate)

            start = time.monotonic()
            try:
                response = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    thinking={"type": "disabled"},
                    messages=messages,
                    **kwargs,
                )
            except Exception as exc:
                _log_call(
                    tier=tier, model=model, purpose=purpose, input_tokens=None,
                    output_tokens=None,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    ok=False, error=str(exc), run_id=run_id,
                )
                raise
            turns += 1
            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            if budget is not None:
                budget.add(in_tok + out_tok)
            _log_call(
                tier=tier, model=model, purpose=purpose, input_tokens=in_tok,
                output_tokens=out_tok,
                duration_ms=int((time.monotonic() - start) * 1000),
                ok=True, error=None, run_id=run_id,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses or force_answer:
                text = "".join(b.text for b in response.content if b.type == "text")
                return ToolLoopResult(text=text, turns=turns, tool_calls=calls, exhausted=exhausted)

            messages.append({"role": "assistant", "content": [_block_to_param(b) for b in response.content]})
            results = []
            for block in tool_uses:
                try:
                    out, is_error = handler(block.name, block.input), False
                    calls.append({"name": block.name, "input": block.input, "error": None})
                except Exception as exc:  # noqa: BLE001 - surfaced to the model
                    out, is_error = f"error: {exc}", True
                    calls.append({"name": block.name, "input": block.input, "error": str(exc)})
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": out, "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})
