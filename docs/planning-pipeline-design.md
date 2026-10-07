# SDLC Planning Pipeline — Design Document (v1, Locked)

**Scope:** Easy-to-medium complexity tasks. Single repo. Human-approved plan output, consumed by executor agents built on Claude Agent SDK.

**Status:** Locked for v1 build. Deep-lane (complex tasks) design exists but is explicitly deferred.

---

## 1. Problem Statement

User submits a feature request or bug report. The system must:

1. Understand the request in the context of the actual codebase
2. Resolve ambiguity (clarifying questions only when genuinely needed)
3. Produce a task DAG with clear dependencies and per-task scope
4. Get human approval before anything executes

**North-star accuracy metric:** plan edit-distance (approved DAG vs. generated DAG) trending toward zero, and post-approval scope-change rate near zero.

---

## 2. Core Design Principles

These are the accuracy levers. Everything in the design exists to serve one of them.

1. **LLM proposes over a closed set; code disposes.** The LLM never emits free-form output that flows unchecked into the next step. File paths are selected from retrieved candidates, never generated from imagination. Deterministic validators check every LLM output against ground truth (symbol graph, pinned SHA).
2. **Grounded context beats bigger prompts.** Accuracy comes from *excluding* irrelevant code under a hard token budget, not stuffing the context window.
3. **Deterministic structure decides what's connected; the LLM decides what's relevant.** Stack traces are parsed by regex, dependencies come from the AST-derived symbol graph, edge augmentation is pure code.
4. **Two human gates, each positioned where humans are cheap.** Humans are excellent at "is this scope right?" and "does this breakdown make sense?", and terrible at generating either. **Amended from v1's single gate** (see §6, §8): scope is approved before any task breakdown is generated, so a wrong assumption is caught before it shapes a DAG built on top of it. The scope screen leads with assumptions and non-goals (the most likely errors); the plan screen leads with the task DAG.
5. **Ambiguity never blocks; it binds.** Every clarifying question ships with a default assumption. Skipped question = default becomes a binding, recorded assumption.
6. **Honest failure beats confident overreach.** If signals say the task is bigger than this pipeline handles, say so — never silently produce a bad plan.

---

## 3. Architecture (Minimal)

```
Request → [Stage A: Context Engine] → [Stage B: Spec+Plan Generation]
              → Validators → Single Approval Screen → Approved Plan artifact
```

**Runtime shape:** one API service + one worker + Postgres. No Temporal/durable orchestration in v1 — a `runs` state-machine table plus a worker queue is sufficient at this complexity.

**State machine (4 states):**
```
context_building → awaiting_approval → approved | failed
                                     ↘ cancelled
```

---

## 4. Prerequisite: Code Index (built at repo onboarding, updated on push)

| Index | Built with | Contents | Purpose |
|---|---|---|---|
| Symbol graph | tree-sitter (language-agnostic AST) | nodes: files/classes/functions; edges: imports, calls, implements | Deterministic blast-radius expansion + edge augmentation |
| Vector index | AST-aware chunking (whole functions/classes + docstring + path), embeddings in pgvector | one chunk per symbol | Conceptual retrieval (features) |
| BM25 index | plain lexical index over code + comments | — | Error strings, literals, config keys |

- **Incremental updates:** webhook on push → reparse changed files only, patch graph edges. Nightly full reindex as reconciliation.
- **Index versioned by commit SHA.** Every run pins to a SHA at creation. All validation happens against that SHA.
- Pre-compute an **arch_summary** per repo (~500 tokens: module map, key entry points) at index time.

**Known trade-off:** static analysis degrades on dynamic languages (Python metaprogramming, dynamic JS imports). Mitigation: vector + BM25 channels cover what the graph misses; retrieval is always a union of signals.

---

## 5. Stage A — Context Engine

**Input:** request text (+ optional attachments: stack traces, logs) + repo@SHA
**Output:** Context Pack artifact (≤ 25k tokens)

### Steps

1. **Entity extraction** — 1 cheap LLM call over request text only:
   ```json
   { "symbols": [...], "error_strings": [...], "files_mentioned": [...],
     "domains": [...], "type": "bug|feature" }
   ```
   Stack frames are parsed **deterministically** (regex per language), never by the LLM.

2. **Retrieval — three parallel channels, union:**
   - Symbol match (exact/fuzzy against graph node names) — code entities
   - BM25 (error strings, identifiers) — literals, log messages
   - Vector (full request text) — conceptually related code with different naming
   - Bug requests weight symbol+BM25; feature requests weight vector.
   - Take top-15 seeds.

3. **Graph expansion:** 1 hop from seeds (callers + callees). Hard cap 25 files. Force-include: stack-frame files (full content) and test files of any seed.

4. **Rerank + pack** — 1 cheap LLM call labels candidates `critical | relevant | peripheral | irrelevant`; then greedy budget packing with tiered fidelity:
   - Tier 1 (full source): seeds, stack-frame files, critical
   - Tier 2 (signatures + docstrings): 1-hop, relevant
   - Tier 3 (path + 1-line summary): peripheral
   - Always include: arch_summary + last 10 commits touching Tier-1 files
   - Never truncate mid-function — demote to AST-extracted relevant functions instead.

### Context Pack schema
```json
{ "sha": "...",
  "files": [{"path","tier","label","reason"}],
  "symbols": [...], "arch_summary": "...",
  "recent_commits": [...], "token_count": 0 }
```

### Fan-out note (kept simple for v1)
Easy-medium tasks have 1–3 concepts; plain union retrieval converges. If retrieval spreads across 3+ unrelated modules, that trips the escalation flag (§9) rather than triggering the deferred anchor/modifier machinery.

---

## 6. Stage B — Spec, then Plan (two strong LLM calls, one human gate between them)

**Amended from v1's single call.** Originally one call produced spec + open_questions + plan together, on the reasoning that a 1–8-criteria spec and a 1–6-task plan are small enough for a strong model to hold coherently in one shot. Still true — but coherence isn't the only cost: a plan built from a spec the human hasn't seen yet means a wrong scope assumption gets baked into task boundaries before anyone catches it. Splitting into `generate_spec` (spec + open_questions only) and, after human approval, `generate_plan` (tasks from the now-frozen spec) moves the catch earlier. The frozen spec is passed to `generate_plan` verbatim — the plan-generation call cannot alter `touchable_files`, `non_goals`, or criteria, only propose tasks over them.

**Input:** request + Context Pack
**Output (single structured response, JSON-schema constrained):**

```json
{
  "spec": {
    "problem_statement": "...",
    "criteria": [ {"id":"AC1","given":"...","when":"...","then":"...",
                   "verify_by":"unit|integration|manual"} ],
    "touchable_files": ["..."],
    "non_goals": ["..."],
    "assumptions": [ {"text":"...","confidence":"high|low"} ]
  },
  "open_questions": [ {"q":"...","default":"..."} ],
  "plan": {
    "tasks": [ {"id":"t1","title":"...","files":[...],
                "done_criteria":"...","est_size":"S|M|L",
                "criterion_refs":["AC1"]} ],
    "edges": [ {"from":"t1","to":"t2","type":"data|code|contract"} ]
  }
}
```

### Prompt rules baked in
- `touchable_files` selected from Context Pack files only; new files (e.g. migrations) allowed but validated (§7).
- `non_goals` = adjacent modules found in the pack but NOT part of the request, written as "we will not touch X because Y". This makes scope creep explicit and machine-checkable.
- `open_questions`: max 3, only for genuinely blocking low-confidence assumptions, each with a default.
- Every criterion must be Given/When/Then and name how it is verified. Unverifiable = not a criterion.
- Task ordering follows layers present in touched files: schema → domain logic → API → tests. Skip absent layers.
- Every criterion must have ≥1 test-bearing task.

---

## 7. Validators (pure code — the accuracy core, never cut)

Run after Stage B, before the approval screen:

1. **Path validity:** every file exists at pinned SHA, or is new with an existing parent dir + naming consistent with sibling files.
2. **Criteria parseability:** G/W/T structure parses; `verify_by` present.
3. **Scope consistency:** no file in both `touchable_files` and a non-goal; every `task.files ⊆ touchable_files`.
4. **Edge augmentation (symbol graph):** if task B's files import/call symbols defined in task A's files and no edge exists → add `code` edge. This catches dependencies the LLM missed.
5. **DAG validity:** topo-sort acyclic check.
6. **Traceability both ways:** every criterion → ≥1 task; every task → ≥1 criterion (no orphan work).
7. **Test coverage:** every criterion reachable by a test task.

**Failure handling:** feed validator errors back verbatim, 1 repair retry, then `failed` with a human-readable reason. Bounded loops only — never infinite self-repair.

---

## 8. Approval (two screens, two checkpoints)

**Amended from v1's single screen** (see §6): scope approval and plan approval are now separate gates, run back to back but independently versioned and independently rejectable.

**Screen 1 — scope** (`awaiting_spec_approval`), most-likely-wrong first:
1. Assumptions + non-goals (plain language)
2. Open questions (inline, defaults pre-filled)
3. Criteria — directly hand-editable (problem statement, criteria, touchable_files, non_goals); an edit is validated deterministically (the 3 spec-scoped checks — path validity, criteria well-formedness, scope consistency) before it's accepted, no LLM call

User actions:
- **Approve scope** → unanswered questions bind to defaults → scope frozen, verbatim, into every downstream artifact → plan generation begins.
- **Answer questions** → one spec regeneration → re-approve.
- **Save edits** → deterministic re-validation, no regeneration, stays on this screen.
- **Reject with feedback** → full spec regeneration.

**Screen 2 — plan** (`awaiting_approval`), shown only after scope approval:
1. Task DAG (visualization + task cards: files, done-criteria, size, deps)
2. A prose Q&A channel over the plan (e.g. "why does t3 depend on t1?") — answers explain, they never mutate the plan in place; a reply that implies a change is a one-click bridge into "Reject with feedback" below, not a silent regeneration.

User actions:
- **Approve** → run `approved`; plan artifact frozen.
- **Reject with feedback** → plan regeneration from the still-frozen spec (the spec is never regenerated from this screen).

---

## 9. Escalation Valve (honest failure)

Trip conditions (computed, not vibes): touchable set > ~12 files, tasks > 8, retrieval spread across 3+ unrelated modules.

Behavior: surface *"this looks larger than this pipeline handles well — proceed anyway or break the request down?"* Never silently produce a plan past these thresholds.

---

## 10. API (4 endpoints)

Conventions: `/v1`, JSON, `Idempotency-Key` required on POSTs (agents retry), problem+json errors, `version` field on mutable artifacts with 409 on stale writes.

```
POST /v1/runs
  { "repo","ref","request":{"type","text","attachments":[...]} }
  → 201 { "run_id","status","sha" }

GET  /v1/runs/{id}
  → snapshot: run + context-pack summary + spec + plan + validation results
    (one call = full state; agents avoid multiple round trips)

POST /v1/runs/{id}/approve
  { "version", "answers": [...]?, "spec_edits": jsonpatch? }
  → approve, or (if answers/edits present) regenerate → back to awaiting_approval

POST /v1/runs/{id}/reject
  { "version", "feedback" }

GET  /v1/runs/{id}/events        → SSE, replayable via ?after=cursor
```

### Claude Agent SDK integration notes
- Endpoints map 1:1 to agent tools (`create_run`, `get_run`, `approve_plan`, `reject_plan`). Schemas above are already tool-use-ready.
- Agents never parse prose: all artifacts structured; validation failures machine-readable.
- Idempotency keys are load-bearing for agent retries.
- Executor agents consume the approved plan artifact: tasks + edges + parallel groups (antichains of the partial order).

---

## 11. Data Model (Postgres)

```
runs(id, repo, sha, status, created_by, created_at, ...)
context_packs(run_id, blob_ref, token_count)
specs(run_id, version, body_jsonb, status: draft|confirmed|superseded)
plans(run_id, version, spec_version, body_jsonb, validation_jsonb,
      status: draft|approved|superseded)
approvals(run_id, actor, action, payload_jsonb, ts)
events(run_id, seq, type, payload_jsonb, ts)     -- replayable log
```

Append-only where practical; edits create versions. Full audit trail for free.

---

## 12. LLM Call Budget (per run)

| # | Model | Purpose |
|---|---|---|
| 1 | cheap | entity extraction |
| 2 | cheap | rerank |
| 3 | strong | spec + questions + plan |
| +1 | strong | validator repair (worst case) |
| +1 | strong | regen after answers/edits (when needed) |

Typical: 3 calls. Worst: 5. All calls through one **LLM gateway** layer: model routing, JSON-schema constrained decoding, retries with jitter, per-run token budget, prompt/response logging keyed to run_id.

---

## 13. How Each Piece Buys Accuracy (summary table)

| Mechanism | Error class it kills |
|---|---|
| Symbol graph + retrieval union | planning from imagination; missed relevant code |
| SHA pinning | stale-index inconsistency; irreproducible runs |
| Closed-set file selection | hallucinated file paths |
| Deterministic stack-frame parsing | corrupted bug context |
| Token budget + tiered packing | relevant signal drowned by noise |
| Questions with binding defaults | silent wrong assumptions; blocked pipelines |
| Non-goals in spec | LLM scope creep ("helpfully" fixing nearby code) |
| Edge augmentation from graph | missed task dependencies → broken execution order |
| Bidirectional traceability check | orphan tasks / uncovered criteria |
| Approval screen ordering | rubber-stamped wrong scope |
| Escalation valve | confident bad plans on oversized tasks |
| Persisted per-stage artifacts | undebuggable failures |

---

## 14. Observability / Accuracy KPIs

- **Plan edit-distance** (generated vs. approved) — north star, should trend down
- File add/remove rate at approval (retrieval precision proxy)
- Question answer-vs-skip rate (question usefulness)
- Post-approval failure/scope-break rate during execution
- Validator failure + repair-success rates per check
- Golden-set regression evals: replay stored runs on any prompt/model change, diff artifacts

---

## 15. Build Order (where to start)

1. **Week 1–2 — Indexing:** tree-sitter parsing → symbol graph; AST chunking → pgvector; BM25; webhook incremental updates; SHA pinning.
2. **Week 2–3 — Stage A:** entity extraction, 3-channel retrieval, expansion, rerank, budget packing. *Test in isolation with golden requests before touching Stage B.*
3. **Week 3–4 — Stage B + validators:** the one-shot prompt, JSON-schema enforcement, all 7 validators, repair loop.
4. **Week 4–5 — API + state machine + events**, then approval UI (thin SPA on the public API — no private endpoints; the webapp is just another client).
5. **Week 5+ — Agent tool definitions** for the executor team; golden-set eval harness.

**Tech choices:** Postgres (+pgvector), tree-sitter, any BM25 (pg trigram/tantivy/meilisearch), FastAPI-or-equivalent, one worker queue (e.g. arq/celery), Anthropic API via gateway layer.

---

## 16. Explicitly Deferred (v2+, do not build now)

- Deep lane: anchor/modifier concept logic, intersection scoring, vertical slicing, multi-round clarification, partial DAG regeneration
- Scope-change request workflow (v1: execution failure → re-plan)
- Durable orchestration (Temporal) — add when runs span days or multi-repo
- Multi-repo planning, learned task sizing, IDE integration
- Jira/Linear export adapters

**Growth guarantee:** artifact schemas (Context Pack, Spec, Plan) are identical between this design and the full pipeline. The Deep lane later slots in as a different *producer* of the same artifacts — approval UI and executor agents never change.
