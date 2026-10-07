"""Tool functions for executor agents (Claude Agent SDK) to consume the
planning pipeline. Each function is a thin, synchronous wrapper over one
HTTP call to the pipeline's `/v1` API (`src/api.py`) — no business logic
here, that all lives server-side.

Base URL: env `PLANNING_API_URL` (default http://localhost:8000).

Every function returns a JSON-serializable dict (or, for `next_ready_tasks`,
a list on success). Nothing raises for an HTTP/network/domain error — those
come back as `{"error": {"status", "title", "detail"}}` so an LLM agent can
branch on the value instead of catching an exception. `client` is an
internal-only kwarg (an `httpx.Client`) for dependency injection in tests;
it is never exposed to the agent via `TOOLS`.

`TOOLS` is a plain JSON-schema tool-definition list (name/description/
input_schema), the shape `claude_agent_sdk.tool()` expects — see
`agent_tools/README.md` for how to wire these into an `SdkMcpServer`.
"""

from __future__ import annotations

import time
import uuid

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"


def _base_url() -> str:
    import os

    return os.environ.get("PLANNING_API_URL", DEFAULT_BASE_URL)


def _is_error(resp: dict) -> bool:
    """True only for OUR request-failure wrapper `{"error": {"status",
    "title", "detail"}}` from `_call` — NOT for a run snapshot that
    legitimately has a top-level `"error"` field (GET /runs/{id}'s `error`
    is the run's own failure message, a plain str|None, never a dict with
    these keys). Checking bare `"error" in resp` would treat every healthy
    snapshot as a failed call."""
    err = resp.get("error") if isinstance(resp, dict) else None
    return isinstance(err, dict) and "status" in err and "title" in err


def _call(method: str, path: str, *, json_body: dict | None = None,
          headers: dict | None = None, client: httpx.Client | None = None) -> dict:
    """One HTTP round trip -> a plain dict. Non-2xx and transport failures
    both come back as {"error": {...}} rather than raising."""
    own = client is None
    c = client or httpx.Client(base_url=_base_url(), timeout=30.0)
    try:
        try:
            resp = c.request(method, path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            return {"error": {"status": None, "title": "request_failed", "detail": str(exc)}}
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {"detail": resp.text}
        if resp.status_code >= 400:
            if isinstance(body, dict):
                detail = body.get("detail", body)
                title = body.get("title", "error")
            else:
                detail, title = body, "error"
            return {"error": {"status": resp.status_code, "title": title, "detail": detail}}
        return body
    finally:
        if own:
            c.close()


# --------------------------------------------------------------------- tools


def create_planning_run(repo_path: str, request_type: str, request_text: str,
                          attachments: list[str] | None = None, *,
                          client: httpx.Client | None = None) -> dict:
    """Start a new planning run for a repo.

    Preconditions: the repo must already be registered and finished indexing
    (status='ready') — check via GET /v1/repos first if unsure; a
    not-ready repo comes back here as an error, not a partial run.

    Use this when the user hands you a bug report or feature request and
    wants a task plan generated for it. Returns immediately — the pipeline
    (context retrieval + spec generation) runs in the background. Poll with
    `wait_for_status` or `get_run` to see when it reaches
    'awaiting_spec_approval' (gate 1, the scope check) — that's the first
    stop, well before the final plan's 'awaiting_approval' (gate 2).

    Args:
        repo_path: absolute filesystem path to the indexed repo root.
        request_type: "bug" or "feature".
        request_text: the request/bug report in plain English.
        attachments: optional raw text blobs (stack traces, logs).

    Returns:
        {"run_id", "status", "sha"} on success, or {"error": {...}}.
    """
    body = {
        "repo_path": repo_path,
        "request": {"type": request_type, "text": request_text, "attachments": attachments or []},
    }
    return _call("POST", "/v1/runs", json_body=body,
                 headers={"Idempotency-Key": str(uuid.uuid4())}, client=client)


def get_run(run_id: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch the full current state of a run in one call: status, request,
    context-pack summary, spec+plan (if generated), and the validation
    report (including any escalation flag). Use this to check progress or
    to inspect open_questions before approving.

    Returns the snapshot dict, or {"error": {...}} if run_id is unknown.
    """
    return _call("GET", f"/v1/runs/{run_id}", client=client)


def get_approved_plan(run_id: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch the plan for a run, but ONLY once it has been approved by a
    human. Use this right before starting execution, as the final check
    that the plan you're about to run tasks from is the approved one.

    Preconditions: run must be in status 'approved' (call `approve_plan`
    first, or wait for a human to approve via the webapp).

    Returns the plan body (spec + open_questions + plan.tasks/edges) on
    success, or {"error": {...}} if the run isn't approved yet or is
    unknown.
    """
    resp = _call("GET", f"/v1/runs/{run_id}/plan", client=client)
    if _is_error(resp):
        return resp
    if resp.get("status") != "approved":
        return {"error": {
            "status": resp.get("status"), "title": "not_approved",
            "detail": f"run {run_id} is {resp.get('status')!r}, not 'approved' yet",
        }}
    return resp["plan"]


def wait_for_status(run_id: str, statuses: list[str], timeout_s: float = 300,
                     poll_s: float = 2, *, client: httpx.Client | None = None,
                     _sleep=time.sleep, _clock=time.monotonic) -> dict:
    """Block (polling `get_run`) until the run reaches one of `statuses`, or
    `timeout_s` elapses. A run's real status sequence is:
    'pending' -> 'context_building' -> 'spec_drafting' -> 'awaiting_spec_approval'
    -> 'planning' -> 'awaiting_approval' -> 'approved'. Use this after
    `create_planning_run` to wait for 'awaiting_spec_approval' (or 'failed');
    after `approve_spec` (no answers) to wait for 'awaiting_approval'; after
    `approve_spec` (with answers) or `reject_spec` to wait for
    'awaiting_spec_approval' again (both regenerate the spec).

    Returns the snapshot dict once a matching status is reached, or
    {"error": {...}} on timeout (with the last snapshot attached) or on any
    request failure encountered while polling.
    """
    deadline = _clock() + timeout_s
    wanted = set(statuses)
    while True:
        snap = get_run(run_id, client=client)
        if _is_error(snap):
            return snap
        if snap.get("status") in wanted:
            return snap
        if _clock() >= deadline:
            return {"error": {
                "status": "timeout", "title": "wait_for_status timed out",
                "detail": f"run {run_id} still {snap.get('status')!r} after {timeout_s}s",
                "last_snapshot": snap,
            }}
        _sleep(poll_s)


def approve_spec(run_id: str, version: int, answers: list[dict] | None = None, *,
                  client: httpx.Client | None = None) -> dict:
    """Gate 1: approve or refine a run's scope (its spec + open questions).

    With no `answers`, this approves the scope as-is and kicks off plan
    generation — the run moves 'awaiting_spec_approval' -> 'planning' ->
    'awaiting_approval'. Every open question left unanswered binds to its
    recorded default automatically.

    With `answers`, the spec is regenerated to incorporate them and the run
    stays at 'awaiting_spec_approval' — call `approve_spec` again (with the
    new version) once you're happy with the regenerated spec.

    Either way, call `wait_for_status` again afterward to know when the run
    has settled.

    Preconditions: run must be in status 'awaiting_spec_approval'; `version`
    must match the current spec version (from `get_run`) or this fails with
    a version-conflict error — always re-fetch and retry with the fresh
    version rather than guessing.

    Args:
        run_id: the run to act on.
        version: the spec version being approved (optimistic lock).
        answers: optional list of {"q_index": int, "answer": str}.

    Returns: {"status": ...}, or {"error": {...}}.
    """
    body: dict = {"version": version}
    if answers:
        body["answers"] = answers
    return _call("POST", f"/v1/runs/{run_id}/spec/approve", json_body=body,
                 headers={"Idempotency-Key": str(uuid.uuid4())}, client=client)


def reject_spec(run_id: str, version: int, feedback: str, *,
                 client: httpx.Client | None = None) -> dict:
    """Gate 1: reject a run's spec with free-text feedback, triggering a full
    spec regeneration. Use this when the scope itself is wrong, not just an
    open question's answer (for that, use `approve_spec` with `answers=`).

    Preconditions: run must be in status 'awaiting_spec_approval'; `version`
    must match the current spec version.

    Returns {"status": ...} or {"error": {...}}.
    """
    body = {"version": version, "feedback": feedback}
    return _call("POST", f"/v1/runs/{run_id}/spec/reject", json_body=body,
                 headers={"Idempotency-Key": str(uuid.uuid4())}, client=client)


def approve_plan(run_id: str, version: int, *,
                  client: httpx.Client | None = None) -> dict:
    """Gate 2: approve a run's final plan, moving it to status 'approved' and
    freezing the plan artifact for execution. Only valid once the run has
    made it through gate 1 to 'awaiting_approval' — scope-level answers and
    edits happen earlier, via `approve_spec` (or a human editing the spec
    directly; there is no agent tool for that, by design).

    Preconditions: run must be in status 'awaiting_approval'; `version` must
    match the current spec_plan version (from `get_run`) or this fails with
    a version-conflict error — always re-fetch and retry with the fresh
    version rather than guessing.

    Args:
        run_id: the run to approve.
        version: the spec_plan version being approved (optimistic lock).

    Returns: {"status": "approved", "version": ...}, or {"error": {...}}.
    """
    body = {"version": version}
    return _call("POST", f"/v1/runs/{run_id}/approve", json_body=body,
                 headers={"Idempotency-Key": str(uuid.uuid4())}, client=client)


def reject_plan(run_id: str, version: int, feedback: str, *,
                 client: httpx.Client | None = None) -> dict:
    """Gate 2: reject a run's final plan with free-text feedback, triggering
    a full plan regeneration (from the still-approved spec) that returns to
    'awaiting_approval'. Use this when the plan's approach is wrong, not the
    scope (for that, go back to `reject_spec` on a fresh run).

    Preconditions: run must be in status 'awaiting_approval'; `version` must
    match the current spec_plan version.

    Returns {"status": ...} or {"error": {...}}.
    """
    body = {"version": version, "feedback": feedback}
    return _call("POST", f"/v1/runs/{run_id}/reject", json_body=body,
                 headers={"Idempotency-Key": str(uuid.uuid4())}, client=client)


def get_task_context(run_id: str, task_id: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch one task from a run's plan plus the FULL current content of
    every file it touches (read live from the repo index, not the — possibly
    summarized — context pack). Use this right before starting work on a
    task: it is everything an executor needs to begin.

    Returns {"task": {...}, "files": {path: content_or_null}}, or
    {"error": {...}} if the run has no plan yet or `task_id` doesn't exist
    in it. A null file content means the path is new (not yet in the index).
    """
    return _call("GET", f"/v1/runs/{run_id}/tasks/{task_id}/context", client=client)


def _ready_tasks(tasks: list[dict], edges: list[dict], completed: set) -> list[str]:
    """Pure topo logic: a task is ready once every task that must finish
    before it (an edge pointing to it) is in `completed`, and it isn't
    itself already completed."""
    prereqs: dict[str, set] = {t["id"]: set() for t in tasks}
    for e in edges:
        if e["to_task"] in prereqs:
            prereqs[e["to_task"]].add(e["from_task"])
    return [
        t["id"] for t in tasks
        if t["id"] not in completed and prereqs.get(t["id"], set()) <= completed
    ]


def next_ready_tasks(run_id: str, completed_task_ids: list[str], *,
                      client: httpx.Client | None = None):
    """Given the set of tasks already completed, return the task ids from
    the run's APPROVED plan that are now unblocked (all their dependency
    edges satisfied) and not yet completed. This is the executor's
    scheduling primitive — call it after each task finishes to get the next
    batch that can run in parallel.

    Preconditions: run must be 'approved' (fetches the plan internally via
    `get_approved_plan`).

    Returns: a list of task ids on success, or {"error": {...}} if the plan
    isn't approved yet / the run is unknown.
    """
    plan = get_approved_plan(run_id, client=client)
    if _is_error(plan):
        return plan
    return _ready_tasks(plan["plan"]["tasks"], plan["plan"]["edges"], set(completed_task_ids))


# ------------------------------------------------------------------- TOOLS


TOOLS = [
    {
        "name": "create_planning_run",
        "description": (
            "Start a new planning run for an already-indexed repo. Use when the user hands you "
            "a bug report or feature request and wants a task plan generated. Returns immediately "
            "with status='pending'/'context_building' — the pipeline runs in the background; "
            "follow up with wait_for_status for 'awaiting_spec_approval' (gate 1), the first stop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "absolute path to the indexed repo root"},
                "request_type": {"type": "string", "enum": ["bug", "feature"]},
                "request_text": {"type": "string", "description": "the request/bug report in plain English"},
                "attachments": {
                    "type": "array", "items": {"type": "string"},
                    "description": "optional raw text blobs (stack traces, logs)",
                },
            },
            "required": ["repo_path", "request_type", "request_text"],
        },
    },
    {
        "name": "get_run",
        "description": (
            "Fetch a run's full current state in one call: status, request, context-pack summary, "
            "spec+plan (if generated), and the validation report (with any escalation flag). Use to "
            "check progress or read open_questions before approving."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "get_approved_plan",
        "description": (
            "Fetch the plan for a run, but only once a human has approved it. Use right before "
            "starting execution as the final check. Errors clearly if the run isn't 'approved' yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "wait_for_status",
        "description": (
            "Poll a run until it reaches one of the given statuses (or time out). Real sequence: "
            "pending -> context_building -> spec_drafting -> awaiting_spec_approval -> planning -> "
            "awaiting_approval -> approved. Use after create_planning_run to wait for "
            "'awaiting_spec_approval'/'failed' (gate 1), then after approve_spec to wait for "
            "'awaiting_approval' (gate 2)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "statuses": {"type": "array", "items": {"type": "string"},
                             "description": "e.g. ['awaiting_approval', 'failed']"},
                "timeout_s": {"type": "number", "default": 300},
                "poll_s": {"type": "number", "default": 2},
            },
            "required": ["run_id", "statuses"],
        },
    },
    {
        "name": "approve_spec",
        "description": (
            "Gate 1: approve or refine a run's scope (spec + open questions). With no `answers`, "
            "approves the scope and starts plan generation ('awaiting_spec_approval' -> 'planning' "
            "-> 'awaiting_approval'). With `answers`, regenerates the spec to incorporate them and "
            "stays at 'awaiting_spec_approval' — call again with the new version. Unanswered open "
            "questions bind to their recorded default automatically. `version` must match the run's "
            "current spec version (re-fetch via get_run on a version-conflict error, never guess)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "version": {"type": "integer"},
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"q_index": {"type": "integer"}, "answer": {"type": "string"}},
                        "required": ["q_index", "answer"],
                    },
                },
            },
            "required": ["run_id", "version"],
        },
    },
    {
        "name": "reject_spec",
        "description": (
            "Gate 1: reject a run's spec with free-text feedback, triggering a full spec "
            "regeneration. Use when the scope itself is wrong (for a merely unanswered question, "
            "use approve_spec with answers instead). `version` must match the run's current spec "
            "version."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "version": {"type": "integer"},
                "feedback": {"type": "string"},
            },
            "required": ["run_id", "version", "feedback"],
        },
    },
    {
        "name": "approve_plan",
        "description": (
            "Gate 2: approve a run's final plan (moves it to 'approved', freezing the plan for "
            "execution). Only valid once the run has reached 'awaiting_approval' — scope-level "
            "answers/edits happen earlier via approve_spec, before the plan exists. `version` must "
            "match the run's current spec_plan version (re-fetch via get_run on a version-conflict "
            "error, never guess)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "version": {"type": "integer"},
            },
            "required": ["run_id", "version"],
        },
    },
    {
        "name": "reject_plan",
        "description": (
            "Gate 2: reject a run's final plan with free-text feedback, triggering a full plan "
            "regeneration from the still-approved spec, back to 'awaiting_approval'. Use when the "
            "plan's approach is wrong, not the scope (for that, reject_spec on a fresh run instead). "
            "`version` must match the run's current spec_plan version."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "version": {"type": "integer"},
                "feedback": {"type": "string"},
            },
            "required": ["run_id", "version", "feedback"],
        },
    },
    {
        "name": "get_task_context",
        "description": (
            "Fetch one task from a run's plan plus the full current content of every file it "
            "touches (read live from the repo index). Use right before starting work on a task — "
            "it is everything needed to begin. A null file content means the path is new."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["run_id", "task_id"],
        },
    },
    {
        "name": "next_ready_tasks",
        "description": (
            "Given the ids of tasks already completed, return the task ids from the run's APPROVED "
            "plan that are now unblocked and not yet completed. Call after each task finishes to get "
            "the next batch that can run (in parallel, if more than one comes back)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "completed_task_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["run_id", "completed_task_ids"],
        },
    },
]

_FN_BY_NAME = {
    "create_planning_run": create_planning_run,
    "get_run": get_run,
    "get_approved_plan": get_approved_plan,
    "wait_for_status": wait_for_status,
    "approve_spec": approve_spec,
    "reject_spec": reject_spec,
    "approve_plan": approve_plan,
    "reject_plan": reject_plan,
    "get_task_context": get_task_context,
    "next_ready_tasks": next_ready_tasks,
}
