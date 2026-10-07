# Task 08: Runs API + State Machine

## Context

Deployment decisions (locked): runs on a server; repos are cloned to server disk out-of-band (human or script does clone/pull) and registered by path via the API; NO auth in v1 (single-team internal tool); executor agents run on the SAME server with filesystem access to the repos.


Done: full generation pipeline (`plan_pipeline` = context → generate → augment/validate → repair). This task wraps it in a persisted, resumable Run with a FastAPI HTTP surface. This is the interface your Claude-Agent-SDK executor agents and the approval UI will consume.

## Files to create

```
src/api.py               # FastAPI app, all routes
src/runs.py              # Run domain logic + state machine + pipeline execution
src/db.py                # ADD tables below
tests/test_runs.py
tests/test_api.py
```

Dependencies: `pip install fastapi uvicorn httpx` (httpx for test client).

## Schema additions

```sql
runs(
  id uuid PK default gen_random_uuid(),
  repo_id int FK,
  status text NOT NULL,          -- see states
  request_type text, request_text text NOT NULL, attachments jsonb DEFAULT '[]',
  error text NULL,
  created_at timestamptz DEFAULT now(), updated_at timestamptz
)
run_artifacts(
  run_id uuid FK ON DELETE CASCADE,
  kind text NOT NULL,            -- context_pack | spec_plan | validation
  version int NOT NULL DEFAULT 1,
  body jsonb NOT NULL,
  created_at timestamptz DEFAULT now(),
  UNIQUE (run_id, kind, version)
)
approvals(
  run_id uuid FK, actor text, action text,    -- approve | reject | answer
  payload jsonb, ts timestamptz DEFAULT now()
)
events(
  run_id uuid FK, seq serial, type text, payload jsonb, ts timestamptz DEFAULT now()
)
-- llm_calls.run_id now wired: pipeline passes run_id through to the gateway
```

## State machine (in src/runs.py, transitions enforced in one place)

```
pending → context_building → planning → awaiting_approval → approved
                                   ↘ failed          ↗ (answers/reject → planning again)
any → cancelled (only from non-terminal)
```
- Single allowed-transitions dict; illegal transition raises. Every transition writes an `events` row and updates `updated_at`.
- Escalation from task 07 does NOT change state; it's carried in the validation artifact and surfaced in responses.

## Execution model

Synchronous-with-thread for v1: `POST /runs` inserts the row (pending), kicks a background thread (`concurrent.futures.ThreadPoolExecutor`, max 4) running the pipeline: → context_building (persist context_pack artifact) → planning (persist spec_plan v1 + validation v1) → awaiting_approval, or failed with error recorded. No celery/queue yet. Process restart loses in-flight runs → on startup, mark any context_building/planning rows as failed with "interrupted" (crash-honesty over silent hang).

## Endpoints

All POSTs require `Idempotency-Key` header: store (key → run_id/response) in a small table; replay returns the original response with 200.

```
POST /v1/repos
  {root_path}
  → validates path exists on server disk and is a git repo (or plain dir — warn);
    upserts repos row; runs index_repo synchronously in the background thread pool;
    201 {repo_id, status}. Bad path → 422 problem+json.

GET /v1/repos                    → list repos with status, indexed_sha, indexed_at, file/symbol counts

POST /v1/repos/{id}/reindex
  → re-runs index_repo (idempotent hash-skip makes this cheap) + build_edges.
    Use after `git pull` on the server. 202 {status: building}.

POST /v1/runs
  {repo_path OR repo_id, request: {type, text, attachments: [str]}}
  → 201 {run_id, status}
  (repo must be status=ready in repos table; else 409 with "index the repo first")

GET /v1/runs/{id}
  → full snapshot: run fields + latest artifact of each kind (context pack
    summarized: paths+tiers only, not full content; spec_plan + validation full)

GET /v1/runs                     → list, filter by status/repo, newest first, limit/offset

POST /v1/runs/{id}/approve
  {version, answers: [{q_index, answer}] | null}
  - 409 if status != awaiting_approval or version != latest spec_plan version
  - answers present → record approval(action=answer), transition → planning,
    background regeneration: repair-style strong call with answers appended
    ("The user answered the open questions: ..."), re-validate, persist v2,
    → awaiting_approval. Response 202 {status: planning}.
  - no answers → approval recorded, unanswered questions bind to defaults
    (record binding in approval payload), → approved. 200.

POST /v1/runs/{id}/reject
  {version, feedback}  → planning; full regeneration with feedback appended; v+1; → awaiting_approval. 202.

POST /v1/runs/{id}/cancel        → cancelled if non-terminal.

GET /v1/runs/{id}/events         → JSON list (seq-ordered). ?after=<seq> for polling.
  (SSE optional; polling endpoint is REQUIRED — agents poll.)

GET /v1/runs/{id}/plan           → convenience: latest approved-or-draft plan only
                                    (what an executor agent fetches to start work)
```

Errors: RFC-7807-style problem+json (`{type,title,status,detail}`) everywhere; validation errors 422; state conflicts 409.

## Tests

test_runs.py (state machine + logic, DB, mocked pipeline):
1. Legal transition path executes with events written per hop.
2. Illegal transitions raise (approved→planning, failed→approved, etc.).
3. Startup recovery marks in-flight as failed.
4. Answers flow: approve-with-answers → v2 artifact created, status returns to awaiting_approval.
5. Defaults binding: approve without answers records the bound defaults in approvals payload; status approved.

test_api.py (httpx TestClient, mocked pipeline function returning canned artifacts):
1. POST /runs happy path 201; unindexed repo 409.
2. Idempotency: same key twice → same run_id, one run row.
3. GET snapshot shape: contains spec_plan, validation, context summary without full file content.
4. approve version conflict → 409; approve wrong state → 409.
5. reject regenerates: version increments, feedback reached the (mocked) regen call.
6. events polling with ?after returns only newer.
7. problem+json shape on a 409 and a 422.

## Real-repo smoke (definition of done, real LLM)

`uvicorn src.api:app` + curl/httpie script: register + index assure42 via `POST /v1/repos {"root_path": "/Users/anuprasjadhav/PycharmProjects/assure42"}`, poll `GET /v1/repos` until ready, POST a run with a realistic feature request, poll GET until awaiting_approval, print snapshot, POST reject with feedback, poll to v2, POST approve. Repeat once on assure42-clinical-ops. Report the full transcript (statuses, versions, validation summaries).

## Out of scope

Auth (single-user local for now), SSE, webapp, rate limiting, async DB, queue system, partial regeneration.
