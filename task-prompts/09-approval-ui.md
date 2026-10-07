# Task 09: Approval UI (thin webapp)

## Context

Done: full API (task 08). This task: a minimal single-page webapp for the two human touchpoints — reviewing/approving plans and submitting runs. It is JUST another API client: no private endpoints, no server-side logic beyond static file serving. Keep it deliberately small — this is an internal tool, not a product.

## Stack & files

Single-file approach, no build step: one static `webapp/index.html` using vanilla JS + fetch (or Preact/htm via CDN if componentization helps — your call, but NO npm build pipeline). Serve it from FastAPI: `GET /` → the file.

```
webapp/index.html
src/api.py            # ADD: static route for /
tests/test_webapp.py  # minimal: / serves html containing the app root
```

## Screens (hash-routed in the one page)

**1. Run list (`#/`)** — table of runs (GET /v1/runs): id (short), status badge, request text (truncated), created. Click → detail. "New run" button.

**2. New run (`#/new`)** — form: repo picker (GET a small added endpoint `GET /v1/repos` listing ready repos — add it to api.py), type toggle bug/feature, request textarea, optional attachment textarea (stack trace). Submit → POST /v1/runs (generate an Idempotency-Key uuid client-side) → redirect to detail.

**3. Run detail (`#/runs/{id}`)** — THE screen that matters. Poll GET snapshot every 2s while status ∈ {pending, context_building, planning}. When awaiting_approval, render in THIS order (most-likely-wrong first, per design):
   1. **Assumptions** (list) and **Non-goals** (list) — visually prominent, top of page.
   2. **Open questions** — each with its default pre-filled in an editable input; untouched = default binds.
   3. **Criteria** — Given/When/Then cards with verify_by badge.
   4. **Touchable files** — plain list.
   5. **Task DAG** — tasks as cards (title, description, files, size, criterion refs) laid out in topological ranks (compute ranks client-side from edges; simple column-per-rank layout; draw edges as an adjacency list under each card: "depends on: t1, t3" — no fancy graph lib).
   6. **Validation report** — checks with pass/fail and details; escalation banner (yellow) if set.
   Buttons: **Approve** (POST approve with version + any edited answers), **Reject** (textarea for feedback → POST reject). On 409 version conflict → toast "plan changed, reloading" + refetch.
   Failed runs: show error + validation details. Approved runs: read-only view of the frozen plan.

## Behavior requirements

- Version field from the snapshot is carried on approve/reject (optimistic concurrency respected).
- While planning after answers/reject, keep polling and show a progress state; on return to awaiting_approval highlight "v2".
- Zero framework state management — a render(state) function re-rendering the page is fine.
- No styling framework needed; a <style> block with sane spacing/badges. Readability over beauty.

## Tests

- tests/test_webapp.py: GET / returns 200 html containing an identifiable root element id.
- Manual test script (documented in the file header of index.html): the full smoke from task 08 but performed through the UI against both repos:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Checklist to report: create run → watch it progress → questions show defaults → edit one answer → approve-with-answers produces v2 → approve → plan frozen. Reject path with feedback produces v2.

## Out of scope

Auth/multi-user, websockets/SSE, graph-drawing libraries, design polish, mobile, npm build tooling.
