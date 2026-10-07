# Task 05: Stage A — Context Engine

## Context

Done so far: extractor, indexer + `src/index_query.py` (find_symbols / search_text / neighbors / tests_of / get_file / get_symbols_in_file), edges, LLM gateway (`src/llm.py`, prompt loader). This task builds Stage A: turn a user request + indexed repo into a **Context Pack** — the grounded slice of the codebase (≤ 25k tokens) that Stage B plans over. Works identically for TS and Python repos.

## Files to create

```
src/models.py            # ADD: ContextPack, PackedFile, ExtractedEntities models
src/context.py           # the whole stage: entities → retrieve → expand → rerank → pack
src/prompts/entity_extraction.md
src/prompts/rerank.md
tests/test_context.py
```

## Data models (pydantic, in src/models.py)

```python
class ExtractedEntities(BaseModel):
    symbols: list[str]; error_strings: list[str]; files_mentioned: list[str]
    domains: list[str]; request_type: Literal["bug","feature"]

class StackFrame(BaseModel):
    path: str; line: int | None

class PackedFile(BaseModel):
    path: str
    tier: Literal[1,2,3]        # 1=full, 2=signatures+docstrings, 3=path+one-liner
    label: Literal["critical","relevant","peripheral"]
    reason: str
    content: str                # tier-appropriate content actually included

class ContextPack(BaseModel):
    repo_id: int; sha: str | None
    request_type: str
    files: list[PackedFile]
    seed_symbols: list[str]     # qualified names
    recent_commits: list[dict]  # sha, message, files (empty if not a git repo)
    token_count: int
```

## Pipeline: `build_context(repo_id, request_text, attachments: list[str] = []) -> ContextPack`

**1. Entities (1 cheap LLM call + deterministic parsing):**
- LLM (prompt file `entity_extraction.md`, schema=ExtractedEntities) over request text only.
- Stack frames parsed by REGEX from attachments, never by the LLM. Patterns for both ecosystems: Python tracebacks (`File "x.py", line N`) and JS/TS stacks (`at fn (src/x.ts:12:5)`, webpack/next paths — strip `webpack-internal:///` and leading `./`). Normalize to repo-relative paths; drop frames whose path isn't in the index.

**2. Retrieval (3 channels, union):**
- Symbol channel: `find_symbols` exact for each extracted symbol; fuzzy fallback if exact empty. Bare names matching >10 symbols → keep only same-domain matches (path contains any domain word), else drop the name.
- Text channel: `search_text` trigram for each error string (exact match matters); FTS for the request text itself.
- Domain channel: FTS with the domain words joined.
- Union hits → **seed files** ranked by: number of distinct channels/entities that hit the file (intersection-first, per design), then score. Cap 15 seed files. Stack-frame files are ALWAYS seeds regardless of ranking.
- Bug requests weight symbol+trigram channels ×1.5; feature requests weight FTS/domain ×1.5.

**3. Expansion:**
- Seed symbols = symbols in seed files that matched (or all exported symbols of a stack-frame file).
- `neighbors(direction="both", min_confidence="resolved")` 1 hop; include `heuristic` only if resolved neighbors < 5. Add `tests_of(seed symbol names)`. Total file cap after expansion: 25. Drop lowest-ranked non-seeds beyond cap.

**4. Rerank (1 cheap LLM call):**
- Prompt file `rerank.md`, input = request + per-file (path, top symbol signatures, docstrings). Output schema: list of {path, label: critical|relevant|peripheral|irrelevant, reason}. Drop irrelevant. Seeds and stack-frame files can be down-labeled but never dropped.

**5. Budget packing (≤ 25_000 tokens, estimate len/3.5):**
- Tier 1 (full content via get_file): stack-frame files, critical.
- Tier 2 (all signatures + docstrings via get_symbols_in_file, joined): relevant.
- Tier 3 (path + reason line): peripheral.
- Greedy by rank; if over budget, demote largest Tier-1 (except stack-frame files) to Tier 2, then drop Tier 3 entries. Never truncate mid-file — demote instead.
- recent_commits: if repo is git, `git log -n 10 --pretty=... -- <tier1 paths>` via subprocess; tolerate failure → [].

Every step logs counts (seeds found per channel, files after expansion, dropped by rerank, final token_count) to a returned `stats` dict on the pack (add field) — needed for tuning.

## Tests (tests/test_context.py)

Unit (no DB, no LLM — mock gateway + index_query):
1. Stack-frame regex: parses Python traceback and Next.js/TS stack samples correctly; ignores node_modules frames; normalizes webpack-internal paths.
2. Intersection ranking: file hit by 3 entities outranks file hit by 1 with higher raw score.
3. Common-name guard: symbol matching >10 defs without domain overlap is dropped.
4. Packing: over-budget scenario demotes largest non-stack-frame Tier-1 to Tier-2; stack-frame file stays Tier-1; nothing truncated mid-file.
5. Rerank protection: seed labeled "irrelevant" by (mocked) LLM is retained as peripheral, not dropped.
6. Bug vs feature weighting changes seed order on a constructed tie.

Integration (DB + mocked LLM, using sample_repo indexed):
7. A fake bug request mentioning `JwtValidator` + a traceback pointing at src/auth/jwt.py → pack contains jwt.py at Tier 1, its test file included, token_count ≤ budget.
8. A feature request "add a variant prop to the button component" (no symbols, domains=[button, component]) → dashboard.tsx surfaces as seed via FTS/domain channel.

## Real-repo smoke (definition of done)

With real LLM (cheap tier) against BOTH indexed repos:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Run 3 realistic requests per repo (make them up from the repo's visible domain — e.g. one bug with a fabricated stack trace pointing at a real file, one feature touching a known component, one vague feature). Print for each: seeds per channel, final file list with tiers, token_count. Manually assert: the obviously-relevant file for each request appears at Tier 1. Report results.

## Out of scope

Vector/embedding channel, arch_summary generation, anchor/modifier concept machinery, multi-round anything, caching.
