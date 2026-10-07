"""Task 08 (+ two-gate amendment): state machine + run domain logic against
the real DB. The LLM gateway is always a FakeGateway — these tests exercise
persistence and state transitions, not generation quality (that's
test_planning_gen.py / test_validators.py's job).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from src import runs
from src.db import get_session
from src.models import (
    Approval,
    Event,
    ExtractedEntities,
    Repo,
    RerankResponse,
    Run,
    Spec,
)

# ------------------------------------------------------------------ fixtures


@pytest.fixture
def repo_id(db_url):
    """A bare 'ready' repo row — no real files indexed. Stage A/B tolerate an
    empty index fine (every index_query call just returns []), and these
    tests care about run persistence, not retrieval quality."""
    session = get_session()
    try:
        repo = Repo(root_path=f"/tmp/repo-{uuid.uuid4()}", status="ready", indexed_sha="deadbeef")
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


def _make_run(session, repo_id, status="pending") -> Run:
    run = Run(repo_id=repo_id, status=status, request_type="feature", request_text="Do the thing")
    session.add(run)
    session.commit()
    return run


def valid_spec_payload(problem: str = "fix things") -> dict:
    """Flat, top-level file names so path_validity is trivially satisfiable
    even against a zero-file index (parent dir '' always exists)."""
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


def valid_plan_payload() -> dict:
    return {
        "tasks": [
            {"id": "t1", "title": "Implement", "description": "Do the thing in app.py.",
             "files": ["app.py"], "done_criteria": "app.py behaves correctly",
             "est_size": "S", "criterion_refs": ["AC1"]},
            {"id": "t2", "title": "Test", "description": "Add tests for it.",
             "files": ["test_app.py"], "done_criteria": "tests pass",
             "est_size": "S", "criterion_refs": ["AC1"]},
        ],
        "edges": [{"from_task": "t1", "to_task": "t2", "kind": "code"}],
    }


def valid_payload(problem: str = "fix things") -> dict:
    """An assembled spec_plan body — what gate 2's artifact looks like."""
    return {**valid_spec_payload(problem), "plan": valid_plan_payload()}


class FakeGateway:
    """Same purpose-dispatch shape as test_planning_gen.py / test_validators.py's
    fakes: entity_extraction/rerank get canned cheap responses; every spec*
    purpose consumes the next spec payload and every plan* purpose the next
    plan payload (last one repeats)."""

    def __init__(self, spec_payloads: list[dict] | None = None,
                 plan_payloads: list[dict] | None = None):
        self.spec_payloads = spec_payloads if spec_payloads is not None else [valid_spec_payload()]
        self.plan_payloads = plan_payloads if plan_payloads is not None else [valid_plan_payload()]
        self.calls: list[dict] = []

    def _nth(self, prefix: str, payloads: list[dict]):
        n = sum(1 for c in self.calls if c["purpose"].startswith(prefix))
        return payloads[min(n - 1, len(payloads) - 1)]

    def complete_json(self, *, schema, purpose, **kwargs):
        self.calls.append({"purpose": purpose, **kwargs})
        if purpose == "entity_extraction":
            return ExtractedEntities(symbols=[], domains=[])
        if purpose == "rerank":
            return RerankResponse(files=[])
        if purpose.startswith("plan"):
            return schema.model_validate(self._nth("plan", self.plan_payloads))
        return schema.model_validate(self._nth("spec", self.spec_payloads))


def _events_for(session, run_id) -> list[Event]:
    return list(session.execute(
        select(Event).where(Event.run_id == run_id).order_by(Event.seq)
    ).scalars().all())


def _seed_gate1(session, run, payload=None, version=1):
    """A run parked at gate 1 with a context pack and a spec artifact."""
    pack = {"repo_id": run.repo_id, "files": [], "token_count": 0}
    if runs.latest_artifact(session, run.id, "context_pack") is None:
        runs.persist_artifact(session, run.id, "context_pack", pack, version=1)
    runs.persist_artifact(session, run.id, "spec", payload or valid_spec_payload(), version=version)
    runs.persist_artifact(session, run.id, "spec_validation",
                          {"passed": True, "checks": [], "escalation": None}, version=version)


# ---------------------------------------------------------- 1. legal path


def test_legal_transition_path_writes_events_per_hop(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="pending")
        hops = ["context_building", "spec_drafting", "awaiting_spec_approval",
                "planning", "awaiting_approval", "approved"]
        for to_status in hops:
            runs.transition(session, run, to_status)

        assert run.status == "approved"
        events = _events_for(session, run.id)
        assert [e.type for e in events] == [
            "pending->context_building", "context_building->spec_drafting",
            "spec_drafting->awaiting_spec_approval", "awaiting_spec_approval->planning",
            "planning->awaiting_approval", "awaiting_approval->approved",
        ]
    finally:
        session.close()


# ------------------------------------------------------- 2. illegal transitions


def test_illegal_transitions_raise(repo_id):
    session = get_session()
    try:
        approved_run = _make_run(session, repo_id, status="approved")
        with pytest.raises(runs.IllegalTransition):
            runs.transition(session, approved_run, "planning")

        failed_run = _make_run(session, repo_id, status="failed")
        with pytest.raises(runs.IllegalTransition):
            runs.transition(session, failed_run, "approved")

        cancelled_run = _make_run(session, repo_id, status="cancelled")
        with pytest.raises(runs.IllegalTransition):
            runs.transition(session, cancelled_run, "context_building")

        # Why apply_spec_edits writes its Event by hand: staying put is not a
        # transition, and asking for one raises.
        at_gate1 = _make_run(session, repo_id, status="awaiting_spec_approval")
        with pytest.raises(runs.IllegalTransition):
            runs.transition(session, at_gate1, "awaiting_spec_approval")
    finally:
        session.close()


# -------------------------------------------------------- 3. startup recovery


def test_startup_recovery_marks_inflight_as_failed(repo_id):
    session = get_session()
    try:
        in_flight_a = _make_run(session, repo_id, status="context_building")
        in_flight_b = _make_run(session, repo_id, status="spec_drafting")
        in_flight_c = _make_run(session, repo_id, status="planning")
        untouched = _make_run(session, repo_id, status="awaiting_spec_approval")

        n = runs.recover_interrupted_runs(session)
        assert n == 3

        for run in (in_flight_a, in_flight_b, in_flight_c, untouched):
            session.refresh(run)

        assert in_flight_a.status == "failed"
        assert in_flight_a.error == "interrupted"
        assert in_flight_b.status == "failed"
        assert in_flight_c.status == "failed"
        assert untouched.status == "awaiting_spec_approval"  # a human gate, not in-flight

        events = _events_for(session, in_flight_b.id)
        assert any(e.type == "interrupted" for e in events)
    finally:
        session.close()


# ------------------------------------------------- 4. gate 1: execute_pipeline


def test_execute_pipeline_stops_at_spec_gate(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="pending")
        runs.execute_pipeline(run.id, gateway_factory=FakeGateway)

        session.expire_all()
        refreshed = runs.get_run(session, run.id)
        assert refreshed.status == "awaiting_spec_approval"

        assert runs.latest_artifact(session, run.id, "spec").version == 1
        assert runs.latest_artifact(session, run.id, "spec_validation").version == 1
        assert runs.latest_artifact(session, run.id, "context_pack") is not None
        # The whole point of gate 1: no plan exists yet.
        assert runs.latest_artifact(session, run.id, "spec_plan") is None
    finally:
        session.close()


# --------------------------------------------------------- 5. gate 1: answers


def test_spec_answers_flow_creates_v2_and_returns_to_spec_gate(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")

        # Two open questions so the answers_block mapping is actually exercised
        # (a single question would pass even with the old positional bug).
        payload = valid_spec_payload()
        payload["open_questions"] = [
            {"q": "Should we log this?", "default": "No, keep it quiet"},
            {"q": "Should we retry on failure?", "default": "No, fail fast"},
        ]
        _seed_gate1(session, run, payload)

        # Answered out of question order, to prove lookup is by q_index, not position.
        answers = [
            {"q_index": 1, "answer": "Yes, retry it"},
            {"q_index": 0, "answer": "Yes, log it"},
        ]
        result = runs.submit_spec_answers(session, run, version=1, answers=answers)
        assert result == {"status": "spec_drafting"}
        assert run.status == "spec_drafting"

        gw = FakeGateway([valid_spec_payload(problem="v2 incorporates the answer")])
        runs.regenerate_spec_with_answers(run.id, answers, base_version=1, gateway_factory=lambda: gw)

        session.expire_all()
        refreshed = runs.get_run(session, run.id)
        assert refreshed.status == "awaiting_spec_approval"

        latest = runs.latest_artifact(session, run.id, "spec")
        assert latest.version == 2
        assert latest.body["spec"]["problem_statement"] == "v2 incorporates the answer"
        assert runs.latest_artifact(session, run.id, "spec_validation").version == 2
        assert runs.latest_artifact(session, run.id, "spec_plan") is None

        spec_call = next(c for c in gw.calls if c["purpose"] == "spec_answers")
        assert "The user answered the open questions" in spec_call["user"]
        assert "Yes, log it" in spec_call["user"]
        assert "Yes, retry it" in spec_call["user"]
        assert "Should we log this?" in spec_call["user"]
        assert "Should we retry on failure?" in spec_call["user"]
    finally:
        session.close()


# ----------------------------------------------------------- 6. gate 1: edits


def _edited_spec(**overrides) -> Spec:
    spec = dict(valid_spec_payload()["spec"])
    spec.update(overrides)
    return Spec.model_validate(spec)


def test_apply_spec_edits_versions_without_transitioning(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        result = runs.apply_spec_edits(
            session, run, version=1, spec=_edited_spec(problem_statement="hand-edited"))

        assert result == {"status": "awaiting_spec_approval", "version": 2}
        assert run.status == "awaiting_spec_approval"

        latest = runs.latest_artifact(session, run.id, "spec")
        assert latest.version == 2
        assert latest.body["spec"]["problem_statement"] == "hand-edited"
        # open questions carry over — they are answered on the answers path
        assert latest.body["open_questions"] == valid_spec_payload()["open_questions"]
        assert runs.latest_artifact(session, run.id, "spec_validation").version == 2

        assert [e.type for e in _events_for(session, run.id)] == ["spec_edited"]
        assert session.execute(
            select(Approval).where(Approval.run_id == run.id, Approval.action == "spec_edit")
        ).scalar_one().payload == {"version": 2}
    finally:
        session.close()


def test_apply_spec_edits_rejects_an_invalid_spec(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        bad = _edited_spec(criteria=[{"id": "AC1", "given": "x", "when": "they click go",
                                      "then": "the app responds correctly", "verify_by": "unit"}])
        with pytest.raises(runs.SpecInvalid) as excinfo:
            runs.apply_spec_edits(session, run, version=1, spec=bad)

        report = excinfo.value.report
        assert not report.passed
        assert any(c.name == "criteria_wellformed" and not c.passed for c in report.checks)
        # nothing persisted, nothing moved
        assert runs.latest_artifact(session, run.id, "spec").version == 1
        assert run.status == "awaiting_spec_approval"
    finally:
        session.close()


# ------------------------------------------------- 7. gate 1: approve / reject


def test_approve_spec_binds_defaults_and_starts_planning(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        result = runs.approve_spec(session, run, version=1)

        assert run.status == "planning"
        assert result["status"] == "planning"
        assert result["bound_defaults"] == [
            {"q_index": 0, "question": "Should we log this?", "bound_default": "No, keep it quiet"}
        ]

        approval = session.execute(
            select(Approval).where(Approval.run_id == run.id, Approval.action == "spec_approve")
        ).scalar_one()
        assert approval.payload["bound_defaults"] == result["bound_defaults"]
    finally:
        session.close()


def test_reject_spec_returns_to_drafting(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        assert runs.reject_spec(session, run, 1, "wrong scope") == {"status": "spec_drafting"}
        assert run.status == "spec_drafting"

        gw = FakeGateway([valid_spec_payload(problem="second attempt")])
        runs.regenerate_spec_full(run.id, "wrong scope", 1, gateway_factory=lambda: gw)

        session.expire_all()
        assert runs.get_run(session, run.id).status == "awaiting_spec_approval"
        latest = runs.latest_artifact(session, run.id, "spec")
        assert latest.version == 2
        assert latest.body["spec"]["problem_statement"] == "second attempt"
        assert "wrong scope" in next(c for c in gw.calls if c["purpose"] == "spec")["user"]
    finally:
        session.close()


def test_approve_spec_version_conflict_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)
        with pytest.raises(runs.VersionConflict):
            runs.approve_spec(session, run, version=2)
    finally:
        session.close()


def test_approve_spec_wrong_state_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="spec_drafting")
        with pytest.raises(runs.InvalidState):
            runs.approve_spec(session, run, version=1)
    finally:
        session.close()


# --------------------------------------------------- 8. gate 2: plan + approve


def test_generate_plan_job_assembles_onto_the_approved_spec(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run, valid_spec_payload(problem="the approved scope"))
        runs.approve_spec(session, run, version=1)
        assert run.status == "planning"

        runs.generate_plan_job(run.id, gateway_factory=FakeGateway)

        session.expire_all()
        assert runs.get_run(session, run.id).status == "awaiting_approval"
        spec_plan = runs.latest_artifact(session, run.id, "spec_plan")
        assert spec_plan.version == 1
        # the approved spec is copied through verbatim by planning._assemble
        assert spec_plan.body["spec"] == valid_spec_payload("the approved scope")["spec"]
        assert [t["id"] for t in spec_plan.body["plan"]["tasks"]] == ["t1", "t2"]
        assert runs.latest_artifact(session, run.id, "validation").version == 1
    finally:
        session.close()


def test_approve_plan_is_a_bare_transition(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        runs.persist_artifact(session, run.id, "spec_plan", valid_payload(), version=1)

        result = runs.approve_plan(session, run, version=1)

        assert run.status == "approved"
        assert result == {"status": "approved", "version": 1}
        approval = session.execute(
            select(Approval).where(Approval.run_id == run.id, Approval.action == "approve")
        ).scalar_one()
        assert "bound_defaults" not in approval.payload   # bound at gate 1, not here
    finally:
        session.close()


def test_reject_plan_regenerates_only_the_plan(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        _seed_gate1(session, run, valid_spec_payload(problem="the approved scope"))
        runs.persist_artifact(session, run.id, "spec_plan",
                              valid_payload("the approved scope"), version=1)
        runs.persist_artifact(session, run.id, "validation", {"passed": True, "checks": []}, version=1)

        assert runs.reject_plan(session, run, 1, "split t1") == {"status": "planning"}
        assert run.status == "planning"

        renamed = valid_plan_payload()
        renamed["tasks"][0]["title"] = "Implement, split"
        gw = FakeGateway(plan_payloads=[renamed])
        runs.regenerate_plan_full(run.id, "split t1", 1, gateway_factory=lambda: gw)

        session.expire_all()
        assert runs.get_run(session, run.id).status == "awaiting_approval"
        latest = runs.latest_artifact(session, run.id, "spec_plan")
        assert latest.version == 2
        assert latest.body["plan"]["tasks"][0]["title"] == "Implement, split"
        # the spec is untouched: still the one approved at gate 1
        assert latest.body["spec"]["problem_statement"] == "the approved scope"
        assert runs.latest_artifact(session, run.id, "spec").version == 1
        assert "split t1" in next(c for c in gw.calls if c["purpose"] == "plan")["user"]
    finally:
        session.close()


def test_approve_plan_version_conflict_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        runs.persist_artifact(session, run.id, "spec_plan", valid_payload(), version=1)
        with pytest.raises(runs.VersionConflict):
            runs.approve_plan(session, run, version=2)
    finally:
        session.close()


def test_approve_plan_wrong_state_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        with pytest.raises(runs.InvalidState):
            runs.approve_plan(session, run, version=1)
    finally:
        session.close()


# ----------------------------------------------------------- 9. gate 2: chat


class FakeChatGateway:
    """chat_turn's only gateway need: complete_text. No schema/repair loop —
    chat never mutates the plan, so nothing here validates a JSON shape."""

    def __init__(self, answer: str = "t1 depends on t2 because ..."):
        self.answer = answer
        self.calls: list[dict] = []

    def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def _seed_gate2(session, run, problem="fix things", version=1):
    """A run parked at gate 2 with a spec_plan + validation artifact."""
    runs.persist_artifact(session, run.id, "spec_plan", valid_payload(problem), version=version)
    runs.persist_artifact(session, run.id, "validation",
                          {"passed": True, "checks": [{"name": "edge_augmentation", "passed": True,
                                                       "details": ["added code edge t1 -> t2: ..."]}]},
                          version=version)


def test_chat_turn_happy_path_persists_both_messages_and_returns_counts(repo_id, monkeypatch):
    monkeypatch.delenv("MAX_CHAT_TURNS", raising=False)
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        _seed_gate2(session, run)

        gw = FakeChatGateway(answer="t2 depends on t1's exported symbols")
        result = runs.chat_turn(session, run, version=1, message="why does t2 depend on t1?",
                                gateway_factory=lambda: gw)

        assert result == {"answer": "t2 depends on t1's exported symbols", "turns_used": 1, "turns_left": 11}

        chat = runs.latest_artifact(session, run.id, "plan_chat")
        assert chat.version == 1
        messages = chat.body["messages"]
        assert len(messages) == 2
        assert messages[0] == {"role": "human", "content": "why does t2 depend on t1?",
                               "plan_version": 1, "ts": messages[0]["ts"]}
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "t2 depends on t1's exported symbols"
        assert messages[1]["plan_version"] == 1

        approval = session.execute(
            select(Approval).where(Approval.run_id == run.id, Approval.action == "chat")
        ).scalar_one()
        assert approval.payload["message"] == "why does t2 depend on t1?"
        assert approval.payload["answer"] == "t2 depends on t1's exported symbols"

        events = _events_for(session, run.id)
        assert any(e.type == "chat_turn" for e in events)

        # the prompt handed to the model carries the edge_augmentation evidence
        assert "added code edge t1 -> t2" in gw.calls[0]["user"]
        assert gw.calls[0]["purpose"] == "plan_chat"
    finally:
        session.close()


def test_chat_turn_wrong_gate_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        gw = FakeChatGateway()
        with pytest.raises(runs.InvalidState):
            runs.chat_turn(session, run, version=1, message="what?", gateway_factory=lambda: gw)
    finally:
        session.close()


def test_chat_turn_version_conflict_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        _seed_gate2(session, run)
        gw = FakeChatGateway()
        with pytest.raises(runs.VersionConflict):
            runs.chat_turn(session, run, version=99, message="what?", gateway_factory=lambda: gw)
    finally:
        session.close()


def test_chat_turn_enforces_cap_with_env_override(repo_id, monkeypatch):
    monkeypatch.setenv("MAX_CHAT_TURNS", "2")
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        _seed_gate2(session, run)
        gw = FakeChatGateway()

        r1 = runs.chat_turn(session, run, version=1, message="q1", gateway_factory=lambda: gw)
        assert r1 == {"answer": gw.answer, "turns_used": 1, "turns_left": 1}
        r2 = runs.chat_turn(session, run, version=1, message="q2", gateway_factory=lambda: gw)
        assert r2 == {"answer": gw.answer, "turns_used": 2, "turns_left": 0}

        with pytest.raises(runs.ChatLimitReached) as excinfo:
            runs.chat_turn(session, run, version=1, message="q3", gateway_factory=lambda: gw)
        assert "plan v1" in str(excinfo.value)
        assert "2 turns" in str(excinfo.value)

        # the cap-breaking call never reached the model
        assert len(gw.calls) == 2
    finally:
        session.close()


def test_chat_turn_history_is_scoped_to_the_current_plan_version(repo_id, monkeypatch):
    """Turns spent against a superseded plan version don't count toward — or
    get replayed into — a new plan version's budget."""
    monkeypatch.setenv("MAX_CHAT_TURNS", "1")
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_approval")
        _seed_gate2(session, run, problem="v1 plan", version=1)

        gw = FakeChatGateway(answer="v1 answer")
        runs.chat_turn(session, run, version=1, message="v1 question", gateway_factory=lambda: gw)
        with pytest.raises(runs.ChatLimitReached):
            runs.chat_turn(session, run, version=1, message="v1 question 2", gateway_factory=lambda: gw)

        # a new plan version (e.g. after a reject/regenerate) resets the budget
        _seed_gate2(session, run, problem="v2 plan", version=2)
        gw2 = FakeChatGateway(answer="v2 answer")
        result = runs.chat_turn(session, run, version=2, message="v2 question", gateway_factory=lambda: gw2)
        assert result == {"answer": "v2 answer", "turns_used": 1, "turns_left": 0}

        # v1's turn was never sent to v2's model call
        assert "v1 question" not in gw2.calls[0]["user"]
    finally:
        session.close()


# ----------------------------------------------- 9. gate 1: chat (may revise)


class ChatFakeGateway:
    """spec_chat_turn's gateway need: one complete_json call, schema
    SpecChatTurn. Unlike FakeGateway above (which dispatches by purpose
    prefix for full spec/plan generation), each call here just returns the
    next queued SpecChatTurn-shaped dict."""

    def __init__(self, turns: list[dict]):
        self.turns = turns
        self.calls: list[dict] = []

    def complete_json(self, *, schema, purpose, **kwargs):
        self.calls.append({"purpose": purpose, **kwargs})
        n = sum(1 for c in self.calls if c["purpose"] == purpose)
        return schema.model_validate(self.turns[min(n - 1, len(self.turns) - 1)])


def test_spec_chat_prose_only_leaves_spec_unversioned(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        gw = ChatFakeGateway([{"answer": "AC1 is unit because it's pure logic.", "spec": None}])
        result = runs.spec_chat_turn(session, run, version=1, message="why is AC1 unit?",
                                     gateway_factory=lambda: gw)

        assert result == {
            "answer": "AC1 is unit because it's pure logic.",
            "spec_version": 1, "spec_changed": False, "turns_used": 1, "turns_left": 11,
        }
        assert runs.latest_artifact(session, run.id, "spec").version == 1

        chat = runs.latest_artifact(session, run.id, "spec_chat")
        messages = chat.body["messages"]
        assert [m["role"] for m in messages] == ["human", "assistant"]
        assert messages[0]["content"] == "why is AC1 unit?"
        assert messages[1]["content"] == "AC1 is unit because it's pure logic."

        approval = session.execute(
            select(Approval).where(Approval.run_id == run.id, Approval.action == "spec_chat")
        ).scalar_one()
        assert approval.payload["spec_changed"] is False
        assert any(e.type == "spec_chat_turn" for e in _events_for(session, run.id))
    finally:
        session.close()


def test_spec_chat_revision_bumps_spec_version(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        revised = valid_spec_payload(problem="now covers admins too")
        gw = ChatFakeGateway([{"answer": "Updated the scope to cover admins.", "spec": revised}])
        result = runs.spec_chat_turn(session, run, version=1, message="AC3 should cover admins too",
                                     gateway_factory=lambda: gw)

        assert result["spec_changed"] is True
        assert result["spec_version"] == 2

        latest = runs.latest_artifact(session, run.id, "spec")
        assert latest.version == 2
        assert latest.body["spec"]["problem_statement"] == "now covers admins too"
        assert runs.latest_artifact(session, run.id, "spec_validation").version == 2

        chat = runs.latest_artifact(session, run.id, "spec_chat")
        messages = chat.body["messages"]
        assert messages[0]["spec_version"] == 1   # the version the reviewer was looking at
        assert messages[1]["spec_version"] == 2   # the version the answer actually produced
    finally:
        session.close()


def test_spec_chat_failed_revision_not_persisted(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        bad_revision = valid_spec_payload()
        bad_revision["spec"]["criteria"][0]["then"] = "works"   # too thin, criteria_wellformed fails
        gw = ChatFakeGateway([{"answer": "Simplified AC1.", "spec": bad_revision}])
        result = runs.spec_chat_turn(session, run, version=1, message="simplify AC1",
                                     gateway_factory=lambda: gw)

        assert result["spec_changed"] is False
        assert result["spec_version"] == 1
        assert "not applied" in result["answer"]
        assert runs.latest_artifact(session, run.id, "spec").version == 1
    finally:
        session.close()


def test_spec_chat_wrong_gate_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="spec_drafting")
        gw = ChatFakeGateway([{"answer": "n/a", "spec": None}])
        with pytest.raises(runs.InvalidState):
            runs.spec_chat_turn(session, run, version=1, message="what?", gateway_factory=lambda: gw)
    finally:
        session.close()


def test_spec_chat_version_conflict_raises(repo_id):
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)
        gw = ChatFakeGateway([{"answer": "n/a", "spec": None}])
        with pytest.raises(runs.VersionConflict):
            runs.spec_chat_turn(session, run, version=99, message="what?", gateway_factory=lambda: gw)
    finally:
        session.close()


def test_spec_chat_enforces_cap_with_env_override(repo_id, monkeypatch):
    monkeypatch.setenv("MAX_CHAT_TURNS", "2")
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)
        gw = ChatFakeGateway([{"answer": "ok", "spec": None}])

        runs.spec_chat_turn(session, run, version=1, message="q1", gateway_factory=lambda: gw)
        runs.spec_chat_turn(session, run, version=1, message="q2", gateway_factory=lambda: gw)

        with pytest.raises(runs.ChatLimitReached):
            runs.spec_chat_turn(session, run, version=1, message="q3", gateway_factory=lambda: gw)

        # the cap-breaking call never reached the model
        assert len(gw.calls) == 2
    finally:
        session.close()


def test_spec_chat_history_survives_a_revision(repo_id, monkeypatch):
    """The one deliberate divergence from gate 2's chat_turn: history here is
    scoped to the RUN, not the spec version, so a revising turn doesn't wipe
    the conversation that led to it."""
    monkeypatch.setenv("MAX_CHAT_TURNS", "3")
    session = get_session()
    try:
        run = _make_run(session, repo_id, status="awaiting_spec_approval")
        _seed_gate1(session, run)

        gw1 = ChatFakeGateway([{"answer": "AC1 is unit because it's pure logic.", "spec": None}])
        runs.spec_chat_turn(session, run, version=1, message="why is AC1 unit?", gateway_factory=lambda: gw1)

        revised = valid_spec_payload(problem="revised")
        gw2 = ChatFakeGateway([{"answer": "Revised the scope.", "spec": revised}])
        result = runs.spec_chat_turn(session, run, version=1, message="revise it",
                                     gateway_factory=lambda: gw2)
        assert result["spec_version"] == 2

        gw3 = ChatFakeGateway([{"answer": "Still there.", "spec": None}])
        result3 = runs.spec_chat_turn(session, run, version=2, message="one more thing",
                                      gateway_factory=lambda: gw3)
        assert result3["turns_used"] == 3   # all three human turns counted against one run-scoped cap

        chat = runs.latest_artifact(session, run.id, "spec_chat")
        assert len(chat.body["messages"]) == 6   # nothing dropped across the version bump
    finally:
        session.close()
