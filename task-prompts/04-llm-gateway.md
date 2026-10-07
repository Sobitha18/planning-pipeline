# Task 04: LLM Gateway

## Context

Steps 1-3 (done): extractor, indexer + query API, edges. This task builds the single choke point through which ALL LLM calls flow. Stage A (context) and Stage B (planning) will depend on it, so it comes first. Uses the Anthropic API via the `anthropic` Python SDK.

## Files to create

```
src/llm.py                # gateway
src/models.py             # pydantic models for every LLM-produced artifact (started here, grown in later tasks)
tests/test_llm.py
```

## Requirements

```python
# src/llm.py
class LLMGateway:
    def __init__(self, api_key: str | None = None):  # default from env ANTHROPIC_API_KEY
        ...

    def complete_json(self, *, tier: str,             # "cheap" | "strong"
                      system: str, user: str,
                      schema: type[BaseModel],        # pydantic model
                      max_tokens: int = 4096,
                      retries: int = 2) -> BaseModel: ...

    def complete_text(self, *, tier, system, user, max_tokens=2048) -> str: ...
```

1. **Model routing:** tier "cheap" → claude-haiku-4-5-20251001-class model, "strong" → a Sonnet-class model. Model IDs live in `src/config.py` (env-overridable: `LLM_CHEAP_MODEL`, `LLM_STRONG_MODEL`) — never hardcoded at call sites.
2. **Structured output enforcement:** `complete_json` instructs the model to reply with ONLY JSON matching the schema (include the JSON schema derived from the pydantic model in the system prompt), strips markdown fences if present, parses, validates with pydantic. On parse/validation failure → retry with the validation error appended to the prompt ("Your previous response failed validation: <errors>. Respond with corrected JSON only."). After `retries` exhausted → raise `LLMValidationError` carrying last raw response.
3. **Transport retries:** exponential backoff + jitter on rate-limit/overloaded/5xx (respect SDK retry if available, else implement; max 3).
4. **Call logging:** every call appends a row to a `llm_calls` table (add to db.py schema): id, ts, tier, model, purpose (caller-supplied string), input_tokens, output_tokens, duration_ms, ok bool, error text NULL, run_id NULL (nullable — wired to runs in task 08). If DB unavailable, log to stderr and continue — logging must never fail a call.
5. **Token budget hook:** optional `budget: TokenBudget` param — a simple class tracking cumulative tokens per run; exceeding budget raises `BudgetExceeded` BEFORE making the call (estimate = len(prompt)/3.5 + max_tokens).
6. **Prompt files:** create `src/prompts/` dir with a loader `load_prompt(name) -> str` reading `src/prompts/<name>.md`. Prompts are files, not string literals (diffable, eval-able). Create the dir with a placeholder README; actual prompts arrive in tasks 05-06.

## Tests (tests/test_llm.py) — NO network calls in unit tests

Mock the anthropic client (inject a fake or monkeypatch).
1. complete_json happy path: fake returns valid JSON → parsed pydantic instance.
2. Fenced JSON (```json ... ```) is stripped and parsed.
3. Invalid JSON then valid on retry → succeeds, 2 calls made, retry prompt contains the validation error.
4. Retries exhausted → LLMValidationError with raw response attached.
5. Tier routing: cheap/strong pick the configured model IDs.
6. BudgetExceeded raised before the client is called when budget insufficient.
7. Logging row written on success and on failure (use sqlite/skip-if-no-DB pattern consistent with earlier tasks, or assert via injected fake logger).

## Live smoke (definition of done, requires ANTHROPIC_API_KEY)

One real call per tier behind `pytest -m live` marker (excluded by default):
`complete_json(tier="cheap", schema=<trivial 2-field model>, ...)` returns a valid instance. Report models used and token counts.

## Out of scope

Streaming, tool-use/MCP passthrough, caching, multi-provider abstraction, async client.
