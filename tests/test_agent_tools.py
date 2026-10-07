"""Mocked-transport tests for agent_tools.planning_tools: no real server, no
DB, no LLM. httpx.MockTransport swaps in a canned handler per test; every
tool function accepts `client=` for exactly this purpose.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent_tools import planning_tools as pt


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


# --------------------------------------------------------- create_planning_run


def test_create_planning_run_happy_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["idempotency_key"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"run_id": "abc123", "status": "pending", "sha": "deadbeef"})

    result = pt.create_planning_run("/repo", "feature", "add a widget", client=_client(handler))

    assert result == {"run_id": "abc123", "status": "pending", "sha": "deadbeef"}
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/runs"
    assert seen["idempotency_key"]  # auto-generated, non-empty
    assert seen["body"] == {
        "repo_path": "/repo",
        "request": {"type": "feature", "text": "add a widget", "attachments": []},
    }


def test_create_planning_run_generates_fresh_key_each_call():
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(201, json={"run_id": "x", "status": "pending", "sha": None})

    client = _client(handler)
    pt.create_planning_run("/repo", "bug", "it crashes", client=client)
    pt.create_planning_run("/repo", "bug", "it crashes", client=client)

    assert keys[0] != keys[1]


# ---------------------------------------------------------------------- get_run


def test_get_run_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/r1"
        # Real GET /runs/{id} snapshots always carry a top-level "error"
        # field (the run's own failure message, None on success) — this
        # must NOT be confused with the {"error": {...}} call-failure shape.
        return httpx.Response(200, json={"run_id": "r1", "status": "awaiting_approval", "error": None})

    result = pt.get_run("r1", client=_client(handler))
    assert result == {"run_id": "r1", "status": "awaiting_approval", "error": None}
    assert not pt._is_error(result)


def test_get_run_404_mapped_to_error_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"type": "about:blank", "title": "Run Not Found",
                                          "status": 404, "detail": "no run with id r1"})

    result = pt.get_run("r1", client=_client(handler))
    assert result["error"]["status"] == 404
    assert result["error"]["title"] == "Run Not Found"
    assert "r1" in result["error"]["detail"]


# ------------------------------------------------------------- get_approved_plan


def test_get_approved_plan_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "run_id": "r1", "status": "approved", "version": 2,
            "plan": {"spec": {}, "open_questions": [], "plan": {"tasks": [], "edges": []}},
        })

    result = pt.get_approved_plan("r1", client=_client(handler))
    assert result == {"spec": {}, "open_questions": [], "plan": {"tasks": [], "edges": []}}


def test_get_approved_plan_not_yet_approved_is_a_clean_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "r1", "status": "awaiting_spec_approval",
                                          "version": 1, "plan": {"spec": {}}})

    result = pt.get_approved_plan("r1", client=_client(handler))
    assert "error" in result
    assert result["error"]["status"] == "awaiting_spec_approval"


def test_get_approved_plan_propagates_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"title": "Version Conflict", "detail": "stale"})

    result = pt.get_approved_plan("r1", client=_client(handler))
    assert result["error"]["status"] == 409


# ------------------------------------------------------------------ wait_for_status


def test_wait_for_status_polls_until_match():
    statuses = iter(["context_building", "spec_drafting", "awaiting_spec_approval"])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        calls.append(status)
        # error=None on every intermediate snapshot, same as the real API —
        # regression check for the "error" key / call-failure collision.
        return httpx.Response(200, json={"run_id": "r1", "status": status, "error": None})

    result = pt.wait_for_status(
        "r1", ["awaiting_spec_approval", "failed"], timeout_s=10, poll_s=0,
        client=_client(handler), _sleep=lambda s: None,
    )
    assert result["status"] == "awaiting_spec_approval"
    assert calls == ["context_building", "spec_drafting", "awaiting_spec_approval"]


def test_wait_for_status_does_not_mistake_a_null_error_field_for_failure():
    """Regression: build_snapshot() always includes a top-level "error" key
    (the run's own failure message, None on success) — wait_for_status must
    not treat every healthy snapshot as a request failure because of it, and
    must keep polling (not bail out on the first non-matching status)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "r1", "status": "context_building", "error": None})

    clock = iter([0, 1, 2, 100])  # past the deadline only on the 4th read

    def counting_sleep(_s):
        calls.append(_s)

    result = pt.wait_for_status(
        "r1", ["awaiting_approval"], timeout_s=5, poll_s=0,
        client=_client(handler), _sleep=counting_sleep, _clock=lambda: next(clock),
    )
    assert result["error"]["status"] == "timeout"
    assert len(calls) == 2  # kept polling across multiple non-matching, non-error snapshots


def test_wait_for_status_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "r1", "status": "planning", "error": None})

    clock = iter([0, 1, 2, 100])  # 4th read is past the deadline

    result = pt.wait_for_status(
        "r1", ["approved"], timeout_s=5, poll_s=0,
        client=_client(handler), _sleep=lambda s: None, _clock=lambda: next(clock),
    )
    assert result["error"]["status"] == "timeout"
    assert result["error"]["last_snapshot"]["status"] == "planning"


def test_wait_for_status_stops_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"title": "Run Not Found", "detail": "gone"})

    result = pt.wait_for_status("r1", ["approved"], client=_client(handler), _sleep=lambda s: None)
    assert result["error"]["status"] == 404


# --------------------------------------------------------------------- approve_spec


def test_approve_spec_happy_path_no_answers_sends_version_only():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"status": "planning"})

    result = pt.approve_spec("r1", 1, client=_client(handler))
    assert result == {"status": "planning"}
    assert seen["path"] == "/v1/runs/r1/spec/approve"
    assert seen["body"] == {"version": 1}
    assert "answers" not in seen["body"]


def test_approve_spec_with_answers_included_in_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"status": "awaiting_spec_approval"})

    answers = [{"q_index": 0, "answer": "yes"}]
    pt.approve_spec("r1", 1, answers=answers, client=_client(handler))
    assert seen["body"] == {"version": 1, "answers": answers}


def test_approve_spec_version_conflict_mapped_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"title": "Version Conflict", "detail": "stale version"})

    result = pt.approve_spec("r1", 99, client=_client(handler))
    assert result["error"]["status"] == 409
    assert result["error"]["title"] == "Version Conflict"


# ---------------------------------------------------------------------- reject_spec


def test_reject_spec_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/r1/spec/reject"
        assert json.loads(request.content) == {"version": 1, "feedback": "wrong approach"}
        return httpx.Response(202, json={"status": "spec_drafting"})

    result = pt.reject_spec("r1", 1, "wrong approach", client=_client(handler))
    assert result == {"status": "spec_drafting"}


# --------------------------------------------------------------------- approve_plan


def test_approve_plan_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/r1/approve"
        assert json.loads(request.content) == {"version": 1}
        return httpx.Response(200, json={"status": "approved", "version": 1})

    result = pt.approve_plan("r1", 1, client=_client(handler))
    assert result["status"] == "approved"


def test_approve_plan_never_sends_an_answers_key():
    """approve_plan dropped its `answers` param entirely (moved to
    approve_spec) — this call must never include the key, since the caller
    can no longer even pass one."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "approved", "version": 1})

    pt.approve_plan("r1", 1, client=_client(handler))
    assert "answers" not in seen["body"]
    assert seen["body"] == {"version": 1}


def test_approve_plan_version_conflict_mapped_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"title": "Version Conflict", "detail": "stale version"})

    result = pt.approve_plan("r1", 99, client=_client(handler))
    assert result["error"]["status"] == 409
    assert result["error"]["title"] == "Version Conflict"


# ---------------------------------------------------------------------- reject_plan


def test_reject_plan_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/r1/reject"
        assert json.loads(request.content) == {"version": 1, "feedback": "wrong approach"}
        return httpx.Response(202, json={"status": "planning"})

    result = pt.reject_plan("r1", 1, "wrong approach", client=_client(handler))
    assert result == {"status": "planning"}


# ------------------------------------------------------------------ get_task_context


def test_get_task_context_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/r1/tasks/t1/context"
        return httpx.Response(200, json={
            "run_id": "r1", "task": {"id": "t1", "files": ["a.py"]},
            "files": {"a.py": "print(1)"},
        })

    result = pt.get_task_context("r1", "t1", client=_client(handler))
    assert result["task"]["id"] == "t1"
    assert result["files"] == {"a.py": "print(1)"}


def test_get_task_context_unknown_task_mapped_to_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"title": "Task Not Found", "detail": "no task t9"})

    result = pt.get_task_context("r1", "t9", client=_client(handler))
    assert result["error"]["status"] == 404


# ---------------------------------------------------------------- next_ready_tasks
# Diamond DAG: t1 -> t2, t1 -> t3, t2 -> t4, t3 -> t4


_DIAMOND_PLAN = {
    "run_id": "r1", "status": "approved", "version": 1,
    "plan": {
        "spec": {}, "open_questions": [],
        "plan": {
            "tasks": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}, {"id": "t4"}],
            "edges": [
                {"from_task": "t1", "to_task": "t2", "kind": "code"},
                {"from_task": "t1", "to_task": "t3", "kind": "code"},
                {"from_task": "t2", "to_task": "t4", "kind": "code"},
                {"from_task": "t3", "to_task": "t4", "kind": "code"},
            ],
        },
    },
}


def _diamond_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_DIAMOND_PLAN)


def test_next_ready_tasks_diamond_dag_progression():
    client = _client(_diamond_handler)

    assert pt.next_ready_tasks("r1", [], client=client) == ["t1"]
    assert pt.next_ready_tasks("r1", ["t1"], client=client) == ["t2", "t3"]
    assert pt.next_ready_tasks("r1", ["t1", "t2"], client=client) == ["t3"]
    assert pt.next_ready_tasks("r1", ["t1", "t2", "t3"], client=client) == ["t4"]


def test_next_ready_tasks_propagates_not_approved_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "r1", "status": "planning", "version": 1, "plan": {}})

    result = pt.next_ready_tasks("r1", [], client=_client(handler))
    assert result["error"]["status"] == "planning"


# --------------------------------------------------------------------------- TOOLS


def test_tools_list_names_match_public_functions():
    names = {t["name"] for t in pt.TOOLS}
    assert names == {
        "create_planning_run", "get_run", "get_approved_plan", "wait_for_status",
        "approve_spec", "reject_spec", "approve_plan", "reject_plan",
        "get_task_context", "next_ready_tasks",
    }
    assert names == set(pt._FN_BY_NAME)


@pytest.mark.parametrize("entry", pt.TOOLS, ids=lambda e: e["name"])
def test_every_tool_has_a_json_schema_input_schema(entry):
    schema = entry["input_schema"]
    assert schema["type"] == "object"
    assert "properties" in schema
    assert entry["description"].strip()
