"""Task 08: the HTTP surface. httpx TestClient, real DB, pipeline entirely
mocked out (via monkeypatching src.runs's background-job functions) — these
tests exercise routing, idempotency, versioning and problem+json shapes, not
generation quality. The background thread pool is swapped for an inline
stand-in so every request is fully settled by the time it responds.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api
from src import runs as runs_mod
from src.db import get_session
from src.models import Repo, Run, SpecChatTurn

# ------------------------------------------------------------------ fixtures


def _valid_spec_payload(problem: str = "fix things") -> dict:
    return {
        "spec": {
            "problem_statement": problem,
            "criteria": [{"id": "AC1", "given": "a user opens the app", "when": "they click go",
                          "then": "the app responds correctly", "verify_by": "unit"}],
            "touchable_files": ["app.py", "test_app.py"],
            "non_goals": [],
            "assumptions": [{"text": "the app is already running", "confidence": "high"}],
        },
        "open_questions": [{
            "q": "Should we log this?",
            "options": ["No, keep it quiet", "Yes, log it"],
            "default": "No, keep it quiet",
        }],
    }


def _valid_payload(problem: str = "fix things") -> dict:
    return {
        **_valid_spec_payload(problem),
        "plan": {
            "tasks": [
                {"id": "t1", "title": "Implement", "description": "Do the thing in app.py.",
                 "files": ["app.py"], "done_criteria": "app.py behaves correctly",
                 "est_size": "S", "criterion_refs": ["AC1"]},
                {"id": "t2", "title": "Test", "description": "Add tests for it.",
                 "files": ["test_app.py"], "done_criteria": "tests pass",
                 "est_size": "S", "criterion_refs": ["AC1"]},
            ],
            "edges": [{"from_task": "t1", "to_task": "t2", "kind": "code"}],
        },
    }


_VALIDATION_OK = {"passed": True, "checks": [], "escalation": None, "repair_attempts": 0}


@pytest.fixture
def repo(db_url):
    session = get_session()
    try:
        repo = Repo(root_path=f"/tmp/api-repo-{uuid.uuid4()}", status="ready", indexed_sha="deadbeef")
        session.add(repo)
        session.commit()
        return {"id": repo.id}
    finally:
        session.close()


class _InlineExecutor:
    """Stands in for the ThreadPoolExecutor: runs the job synchronously, in
    the request thread, so tests never race a background thread."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "EXECUTOR", _InlineExecutor())
    with TestClient(api.app) as c:
        yield c


def _persist_spec(session, run, problem, version=None):
    v = runs_mod.persist_artifact(session, run.id, "spec", _valid_spec_payload(problem), version=version)
    runs_mod.persist_artifact(session, run.id, "spec_validation", _VALIDATION_OK, version=v)
    return v


def _persist_plan(session, run, problem, version=None):
    v = runs_mod.persist_artifact(session, run.id, "spec_plan", _valid_payload(problem), version=version)
    runs_mod.persist_artifact(session, run.id, "validation", _VALIDATION_OK, version=v)
    return v


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    """Replaces every real-LLM background job with one that persists a
    canned artifact and walks the state machine, exactly like the real job
    would structurally do. Gate 1's jobs stop at awaiting_spec_approval;
    generate_plan_job is what carries a run on to awaiting_approval."""

    def fake_execute(run_id, **kwargs):
        session = get_session()
        try:
            run = session.get(Run, run_id)
            runs_mod.transition(session, run, "context_building")
            runs_mod.persist_artifact(session, run.id, "context_pack", {
                "repo_id": run.repo_id, "sha": "deadbeef", "token_count": 42,
                "files": [{"path": "app.py", "tier": 1, "label": "critical",
                           "reason": "seed", "content": "print('hi')"}],
            }, version=1)
            runs_mod.transition(session, run, "spec_drafting")
            _persist_spec(session, run, "fix things", version=1)
            runs_mod.transition(session, run, "awaiting_spec_approval")
        finally:
            session.close()

    def fake_generate_plan(run_id, **kwargs):
        session = get_session()
        try:
            run = session.get(Run, run_id)
            _persist_plan(session, run, "fix things", version=1)
            runs_mod.transition(session, run, "awaiting_approval")
        finally:
            session.close()

    def fake_regen_spec_answers(run_id, answers, base_version, **kwargs):
        session = get_session()
        try:
            run = session.get(Run, run_id)
            _persist_spec(session, run, "answers applied")
            runs_mod.transition(session, run, "awaiting_spec_approval")
        finally:
            session.close()

    def fake_regen_spec_full(run_id, feedback, base_version, **kwargs):
        session = get_session()
        try:
            run = session.get(Run, run_id)
            _persist_spec(session, run, f"respec: {feedback}")
            runs_mod.transition(session, run, "awaiting_spec_approval")
        finally:
            session.close()

    def fake_regen_plan_full(run_id, feedback, base_version, **kwargs):
        session = get_session()
        try:
            run = session.get(Run, run_id)
            _persist_plan(session, run, f"redo: {feedback}")
            runs_mod.transition(session, run, "awaiting_approval")
        finally:
            session.close()

    monkeypatch.setattr(runs_mod, "execute_pipeline", fake_execute)
    monkeypatch.setattr(runs_mod, "generate_plan_job", fake_generate_plan)
    monkeypatch.setattr(runs_mod, "regenerate_spec_with_answers", fake_regen_spec_answers)
    monkeypatch.setattr(runs_mod, "regenerate_spec_full", fake_regen_spec_full)
    monkeypatch.setattr(runs_mod, "regenerate_plan_full", fake_regen_plan_full)


def _create_run(client, repo, text="Add a widget") -> str:
    """A run parked at gate 1 (awaiting_spec_approval, spec v1)."""
    resp = client.post(
        "/v1/runs",
        json={"repo_id": repo["id"], "request": {"type": "feature", "text": text}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["run_id"]


def _create_run_to_gate2(client, repo, text="Add a widget") -> str:
    """...and on through the scope gate to gate 2 (awaiting_approval,
    spec_plan v1) — what every plan-level test needs."""
    run_id = _create_run(client, repo, text)
    resp = client.post(
        f"/v1/runs/{run_id}/spec/approve", json={"version": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202, resp.text
    return run_id


# --------------------------------------------------------------- 1. POST /runs


def test_create_run_happy_path(client, repo):
    resp = client.post(
        "/v1/runs",
        json={"repo_id": repo["id"], "request": {"type": "feature", "text": "Add a widget"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["run_id"])
    assert body["sha"] == "deadbeef"


def test_create_run_unindexed_repo_409(client, db_url):
    session = get_session()
    try:
        not_ready = Repo(root_path=f"/tmp/notready-{uuid.uuid4()}", status="building")
        session.add(not_ready)
        session.commit()
        repo_id = not_ready.id
    finally:
        session.close()

    resp = client.post(
        "/v1/runs",
        json={"repo_id": repo_id, "request": {"type": "feature", "text": "x"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")


# --------------------------------------------------------------- 2. idempotency


def test_idempotency_same_key_same_run_one_row(client, repo):
    key = str(uuid.uuid4())
    body = {"repo_id": repo["id"], "request": {"type": "feature", "text": "Add a widget"}}

    r1 = client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})
    r2 = client.post("/v1/runs", json=body, headers={"Idempotency-Key": key})

    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert r1.json() == r2.json()

    session = get_session()
    try:
        rows = session.execute(select(Run).where(Run.repo_id == repo["id"])).scalars().all()
        assert len(rows) == 1
    finally:
        session.close()


# --------------------------------------------------------------- 3. snapshot shape


def test_get_run_snapshot_shape_at_spec_gate(client, repo):
    run_id = _create_run(client, repo)
    snap = client.get(f"/v1/runs/{run_id}").json()

    assert snap["status"] == "awaiting_spec_approval"
    assert snap["spec"]["version"] == 1
    assert snap["spec"]["spec"]["problem_statement"] == "fix things"
    assert snap["spec_validation"] == {"version": 1, **_VALIDATION_OK}
    assert snap["spec_plan"] is None       # no plan exists before the scope gate
    assert snap["validation"] is None

    files = snap["context_pack"]["files"]
    assert files == [{"path": "app.py", "tier": 1, "label": "critical"}]
    for f in files:
        assert "content" not in f
        assert "reason" not in f


def test_get_run_snapshot_shape_at_plan_gate(client, repo):
    run_id = _create_run_to_gate2(client, repo)
    snap = client.get(f"/v1/runs/{run_id}").json()

    assert snap["status"] == "awaiting_approval"
    assert snap["spec"]["version"] == 1              # the approved spec stays visible
    assert snap["spec_plan"]["version"] == 1
    assert snap["spec_plan"]["spec"]["problem_statement"] == "fix things"
    assert snap["validation"] == {"version": 1, **_VALIDATION_OK}


# --------------------------------------------------------------- 3b. spec gate


def test_spec_edit_persists_v2_without_changing_status(client, repo):
    run_id = _create_run(client, repo)
    edited = _valid_spec_payload("hand-edited")["spec"]

    resp = client.post(
        f"/v1/runs/{run_id}/spec", json={"version": 1, "spec": edited},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "awaiting_spec_approval", "version": 2}

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert snap["status"] == "awaiting_spec_approval"
    assert snap["spec"]["version"] == 2
    assert snap["spec"]["spec"]["problem_statement"] == "hand-edited"


def test_spec_edit_invalid_422_carries_checks(client, repo):
    run_id = _create_run(client, repo)
    bad = _valid_spec_payload()["spec"]
    bad["criteria"] = [{"id": "AC1", "given": "x", "when": "they click go",
                        "then": "the app responds correctly", "verify_by": "unit"}]

    resp = client.post(
        f"/v1/runs/{run_id}/spec", json={"version": 1, "spec": bad},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["title"] == "Spec Invalid"
    failed = [c for c in body["checks"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["criteria_wellformed"]
    assert failed[0]["details"]

    # nothing persisted
    assert client.get(f"/v1/runs/{run_id}").json()["spec"]["version"] == 1


def test_spec_edit_malformed_body_422(client, repo):
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/spec", json={"version": 1, "spec": {"problem_statement": "only this"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_spec_approve_with_answers_regenerates_and_stays_at_gate1(client, repo, monkeypatch):
    run_id = _create_run(client, repo)
    seen = {}
    original = runs_mod.regenerate_spec_with_answers

    def spy(run_id_, answers, base_version, **kwargs):
        seen["answers"] = answers
        seen["base_version"] = base_version
        original(run_id_, answers, base_version, **kwargs)

    monkeypatch.setattr(runs_mod, "regenerate_spec_with_answers", spy)

    resp = client.post(
        f"/v1/runs/{run_id}/spec/approve",
        json={"version": 1, "answers": [{"q_index": 0, "answer": "yes, log it"}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "spec_drafting"}
    assert seen == {"answers": [{"q_index": 0, "answer": "yes, log it"}], "base_version": 1}

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert snap["status"] == "awaiting_spec_approval"       # back at gate 1, not gate 2
    assert snap["spec"]["version"] == 2
    assert snap["spec"]["spec"]["problem_statement"] == "answers applied"
    assert snap["spec_plan"] is None


def test_spec_approve_without_answers_kicks_plan_generation(client, repo):
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/spec/approve", json={"version": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "planning"
    assert resp.json()["bound_defaults"] == [
        {"q_index": 0, "question": "Should we log this?", "bound_default": "No, keep it quiet"}
    ]

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert snap["status"] == "awaiting_approval"
    assert snap["spec_plan"]["version"] == 1


def test_spec_reject_regenerates_the_spec(client, repo):
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/spec/reject", json={"version": 1, "feedback": "wrong scope"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "spec_drafting"}

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert snap["status"] == "awaiting_spec_approval"
    assert snap["spec"]["version"] == 2
    assert snap["spec"]["spec"]["problem_statement"] == "respec: wrong scope"


def test_spec_approve_version_conflict_409(client, repo):
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/spec/approve", json={"version": 99},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409


# ------------------------------------------------------- 4. approve conflicts


def test_approve_version_conflict_409(client, repo):
    run_id = _create_run_to_gate2(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/approve", json={"version": 99},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409


def test_approve_wrong_state_409(client, repo):
    """Approving the plan at the spec gate is a state error, not a version
    one — there is no plan to approve yet."""
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/approve", json={"version": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    assert resp.json()["title"] == "Invalid State"


def test_approve_plan_approves(client, repo):
    run_id = _create_run_to_gate2(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/approve", json={"version": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "approved", "version": 1}
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "approved"


# -------------------------------------------------------------------- 5. reject


def test_reject_regenerates_version_and_feedback_reaches_regen(client, repo, monkeypatch):
    run_id = _create_run_to_gate2(client, repo)
    seen = {}
    original = runs_mod.regenerate_plan_full

    def spying_regen_full(run_id_, feedback, base_version, **kwargs):
        seen["feedback"] = feedback
        seen["base_version"] = base_version
        original(run_id_, feedback, base_version, **kwargs)

    monkeypatch.setattr(runs_mod, "regenerate_plan_full", spying_regen_full)

    resp = client.post(
        f"/v1/runs/{run_id}/reject", json={"version": 1, "feedback": "wrong approach"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202
    assert seen["feedback"] == "wrong approach"
    assert seen["base_version"] == 1

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert snap["spec_plan"]["version"] == 2
    assert snap["status"] == "awaiting_approval"
    assert snap["spec"]["version"] == 1       # rejecting a plan never touches the spec


# ------------------------------------------------------------- 5b. task context


def test_get_task_context_happy_path(client, repo, db_url):
    run_id = _create_run_to_gate2(client, repo)
    session = get_session()
    try:
        from src.models import File
        session.add(File(repo_id=repo["id"], path="app.py", language="python",
                          content_hash="x", loc=1, content="print('hi')"))
        session.commit()
    finally:
        session.close()

    resp = client.get(f"/v1/runs/{run_id}/tasks/t1/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["id"] == "t1"
    assert body["files"] == {"app.py": "print('hi')"}


def test_get_task_context_unknown_task_404(client, repo):
    run_id = _create_run_to_gate2(client, repo)
    resp = client.get(f"/v1/runs/{run_id}/tasks/does-not-exist/context")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


# ------------------------------------------------------------- 6. events polling


def test_events_polling_with_after(client, repo):
    run_id = _create_run_to_gate2(client, repo)

    all_events = client.get(f"/v1/runs/{run_id}/events").json()
    # pending -> context_building -> spec_drafting -> awaiting_spec_approval
    # -> planning -> awaiting_approval
    assert [e["type"] for e in all_events] == [
        "pending->context_building", "context_building->spec_drafting",
        "spec_drafting->awaiting_spec_approval", "spec_approved",
        "planning->awaiting_approval",
    ]
    assert all_events == sorted(all_events, key=lambda e: e["seq"])

    cursor = all_events[0]["seq"]
    newer = client.get(f"/v1/runs/{run_id}/events", params={"after": cursor}).json()
    assert len(newer) == len(all_events) - 1
    assert all(e["seq"] > cursor for e in newer)


# ------------------------------------------------------------ 7. problem+json


def test_problem_json_shape_on_409(client, repo):
    run_id = _create_run_to_gate2(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/approve", json={"version": 99},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert set(body.keys()) == {"type", "title", "status", "detail"}
    assert body["status"] == 409
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_problem_json_shape_on_422(client):
    resp = client.post(
        "/v1/repos", json={"root_path": "/no/such/path/at/all"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body.keys()) == {"type", "title", "status", "detail"}
    assert resp.headers["content-type"].startswith("application/problem+json")


# ------------------------------------------------------------------- 8. chat


@pytest.fixture
def fake_chat(monkeypatch):
    """chat_turn calls answer_plan_question (not gateway.complete_text
    directly) at module-global lookup time, so patching it here skips the
    real LLMGateway() network call while exercising chat_turn's own logic
    (idempotency, versioning, cap enforcement) for real."""
    calls = []

    def fake_answer(request_text, spec_plan, validation_body, history, question, gateway, **kwargs):
        calls.append(question)
        return f"answer #{len(calls)} to: {question}"

    monkeypatch.setattr(runs_mod, "answer_plan_question", fake_answer)
    return calls


def test_chat_happy_path_200(client, repo, fake_chat):
    run_id = _create_run_to_gate2(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/chat", json={"version": 1, "message": "why does t2 depend on t1?"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "answer #1 to: why does t2 depend on t1?"
    assert body["turns_used"] == 1
    assert body["turns_left"] == 11
    assert fake_chat == ["why does t2 depend on t1?"]

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert len(snap["chat"]) == 2
    assert snap["chat"][0]["role"] == "human"
    assert snap["chat"][1]["role"] == "assistant"


def test_chat_idempotency_replays_without_a_second_llm_call(client, repo, fake_chat):
    run_id = _create_run_to_gate2(client, repo)
    key = str(uuid.uuid4())
    body = {"version": 1, "message": "why?"}

    r1 = client.post(f"/v1/runs/{run_id}/chat", json=body, headers={"Idempotency-Key": key})
    r2 = client.post(f"/v1/runs/{run_id}/chat", json=body, headers={"Idempotency-Key": key})

    assert r1.status_code == 200
    assert r1.json() == r2.json()
    assert len(fake_chat) == 1   # the retry never reached answer_plan_question


def test_chat_409_at_cap(client, repo, fake_chat, monkeypatch):
    monkeypatch.setenv("MAX_CHAT_TURNS", "1")
    run_id = _create_run_to_gate2(client, repo)

    r1 = client.post(
        f"/v1/runs/{run_id}/chat", json={"version": 1, "message": "q1"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r1.status_code == 200
    assert r1.json()["turns_left"] == 0

    r2 = client.post(
        f"/v1/runs/{run_id}/chat", json={"version": 1, "message": "q2"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r2.status_code == 409
    assert r2.headers["content-type"].startswith("application/problem+json")
    assert r2.json()["title"] == "Chat Limit Reached"


def test_chat_409_wrong_gate(client, repo, fake_chat):
    run_id = _create_run(client, repo)   # parked at gate 1, not gate 2
    resp = client.post(
        f"/v1/runs/{run_id}/chat", json={"version": 1, "message": "why?"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    assert resp.json()["title"] == "Invalid State"


# --------------------------------------------------------------- 9. spec chat


@pytest.fixture
def fake_spec_chat(monkeypatch):
    """Same idea as fake_chat above: spec_chat_turn calls answer_spec_question
    at module-global lookup time, so patching it skips the real LLMGateway()
    call while exercising spec_chat_turn's own logic (idempotency, versioning,
    cap enforcement, the revise-or-not branch) for real. Default fake never
    revises — a test that needs a revision overrides the patch itself."""
    calls = []

    def fake_answer(request_text, pack, spec_output, validation_body, history, question, gateway, **kwargs):
        calls.append(question)
        return SpecChatTurn(answer=f"answer #{len(calls)} to: {question}", spec=None)

    monkeypatch.setattr(runs_mod, "answer_spec_question", fake_answer)
    return calls


def test_spec_chat_happy_path_200(client, repo, fake_spec_chat):
    run_id = _create_run(client, repo)
    resp = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "why is AC1 unit?"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "answer #1 to: why is AC1 unit?"
    assert body["spec_changed"] is False
    assert body["spec_version"] == 1
    assert body["turns_used"] == 1
    assert fake_spec_chat == ["why is AC1 unit?"]

    snap = client.get(f"/v1/runs/{run_id}").json()
    assert len(snap["spec_chat"]) == 2
    assert snap["spec_chat"][0]["role"] == "human"
    assert snap["spec_chat"][1]["role"] == "assistant"


def test_spec_chat_idempotency_replays_without_a_second_llm_call(client, repo, fake_spec_chat):
    run_id = _create_run(client, repo)
    key = str(uuid.uuid4())
    body = {"version": 1, "message": "why?"}

    r1 = client.post(f"/v1/runs/{run_id}/spec/chat", json=body, headers={"Idempotency-Key": key})
    r2 = client.post(f"/v1/runs/{run_id}/spec/chat", json=body, headers={"Idempotency-Key": key})

    assert r1.status_code == 200
    assert r1.json() == r2.json()
    assert len(fake_spec_chat) == 1   # the retry never reached answer_spec_question


def test_spec_chat_409_at_cap(client, repo, fake_spec_chat, monkeypatch):
    monkeypatch.setenv("MAX_CHAT_TURNS", "1")
    run_id = _create_run(client, repo)

    r1 = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "q1"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r1.status_code == 200
    assert r1.json()["turns_left"] == 0

    r2 = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "q2"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r2.status_code == 409
    assert r2.headers["content-type"].startswith("application/problem+json")
    assert r2.json()["title"] == "Chat Limit Reached"


def test_spec_chat_409_wrong_gate(client, repo, fake_spec_chat):
    run_id = _create_run_to_gate2(client, repo)   # already past gate 1
    resp = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "why?"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    assert resp.json()["title"] == "Invalid State"


def test_spec_chat_409_stale_version_after_a_revision(client, repo, monkeypatch):
    """A turn that revises the spec bumps its version; a second request still
    targeting the version the reviewer was originally looking at is now stale
    — same 409 story as saveSpecEdits/submitSpecAnswers."""
    run_id = _create_run(client, repo)

    def fake_answer(request_text, pack, spec_output, validation_body, history, question, gateway, **kwargs):
        revised = spec_output.model_copy(update={
            "spec": spec_output.spec.model_copy(update={"problem_statement": "revised"}),
        })
        return SpecChatTurn(answer="Revised it.", spec=revised)

    monkeypatch.setattr(runs_mod, "answer_spec_question", fake_answer)

    r1 = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "revise it"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["spec_changed"] is True
    assert r1.json()["spec_version"] == 2

    r2 = client.post(
        f"/v1/runs/{run_id}/spec/chat", json={"version": 1, "message": "another question"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r2.status_code == 409
    assert r2.json()["title"] == "Version Conflict"
