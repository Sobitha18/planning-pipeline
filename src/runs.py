"""Run domain logic: the persisted state machine wrapped around
`src.planning`'s two-gate pipeline, plus everything an HTTP layer needs to
drive it (artifact versions, snapshots, idempotency replay) without touching
SQL itself. `src/api.py` is a thin FastAPI shell over this module — it owns
the background thread pool and HTTP status-code mapping; every DB write and
every business rule lives here.

State machine (single allowed-transitions dict; illegal transition raises):

    pending -> context_building -> spec_drafting -> awaiting_spec_approval
                                        ^                    |
                                        +--------------------+  (answers/reject)

            -> planning -> awaiting_approval -> approved
                   ^               |
                   +---------------+                          (reject)

    any non-terminal -> cancelled

Gate 1 (`awaiting_spec_approval`) approves the scope; only then is a plan
generated at all. A deterministic spec EDIT stays in `awaiting_spec_approval`
— it persists a new `spec` version and writes its own `Event` instead of
transitioning (`transition()` rejects a self-transition, correctly).

Every transition writes an `events` row and bumps `updated_at` — free audit
trail, and what `GET /runs/{id}/events` polls.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src import index_query as iq
from src.config import get_settings
from src.context import build_context
from src.db import get_session
from src.llm import LLMGateway, load_prompt
from src.models import (
    Approval,
    ContextPack,
    Event,
    IdempotencyKey,
    Repo,
    Run,
    RunArtifact,
    Spec,
    SpecOutput,
    SpecPlanOutput,
    SpecResult,
    ValidationReport,
)
from src.planning import (
    _escalation_message,
    answer_plan_question,
    escalation_for_spec,
    plan_pipeline,
    render_user_prompt,
    spec_pipeline,
    validate_spec,
)

TERMINAL = {"approved", "failed", "cancelled"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"context_building", "failed", "cancelled"},
    "context_building": {"spec_drafting", "failed", "cancelled"},
    "spec_drafting": {"awaiting_spec_approval", "failed", "cancelled"},
    "awaiting_spec_approval": {"spec_drafting", "planning", "cancelled"},
    "planning": {"awaiting_approval", "failed", "cancelled"},
    "awaiting_approval": {"planning", "approved", "cancelled"},
    "approved": set(),
    "failed": set(),
    "cancelled": set(),
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------- domain errors
# api.py maps these 1:1 to HTTP status via a single exception handler — see
# DomainError subclasses' `status_code`/`title` there. Kept here (not in
# api.py) because raising them is a business decision, not a transport one.


class DomainError(Exception):
    pass


class RunNotFound(DomainError):
    pass


class RepoNotFound(DomainError):
    pass


class InvalidRepoPath(DomainError):
    pass


class RepoNotReady(DomainError):
    pass


class IllegalTransition(DomainError):
    pass


class VersionConflict(DomainError):
    pass


class InvalidState(DomainError):
    pass


class TaskNotFound(DomainError):
    pass


class SpecInvalid(DomainError):
    """A hand-edited spec failed `validate_spec`. Carries the report so the
    HTTP layer can hand the failing checks back to the UI verbatim."""

    def __init__(self, message: str, report: ValidationReport):
        super().__init__(message)
        self.report = report


class ChatLimitReached(DomainError):
    """MAX_CHAT_TURNS turns already used against this plan version."""


# --------------------------------------------------------------- transitions


def transition(session, run: Run, to_status: str, *, event_type: str | None = None,
                payload: dict | None = None) -> None:
    allowed = ALLOWED_TRANSITIONS.get(run.status, set())
    if to_status not in allowed:
        raise IllegalTransition(f"cannot transition run {run.id} from {run.status!r} to {to_status!r}")
    from_status = run.status
    run.status = to_status
    run.updated_at = _now()
    session.add(Event(run_id=run.id, type=event_type or f"{from_status}->{to_status}", payload=payload or {}))
    session.commit()


def recover_interrupted_runs(session=None) -> int:
    """Startup recovery: a process restart loses in-flight background work, so
    anything left in context_building/spec_drafting/planning is an orphaned
    run, not a live one. Crash-honesty over a silent hang — mark them failed."""
    own_session = session is None
    session = session or get_session()
    try:
        rows = session.execute(
            select(Run).where(Run.status.in_(["context_building", "spec_drafting", "planning"]))
        ).scalars().all()
        for run in rows:
            run.error = "interrupted"
            transition(session, run, "failed", event_type="interrupted",
                       payload={"reason": "process restart while in-flight"})
        return len(rows)
    finally:
        if own_session:
            session.close()


# --------------------------------------------------------------------- runs


def create_run(session, repo_id: int, request_type: str | None, request_text: str,
                attachments: list[str] | None = None) -> Run:
    run = Run(repo_id=repo_id, status="pending", request_type=request_type,
              request_text=request_text, attachments=list(attachments or []))
    session.add(run)
    session.commit()
    return run


def get_run(session, run_id) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise RunNotFound(f"no run with id {run_id}")
    return run


def list_runs(session, *, status: str | None = None, repo_id: int | None = None,
               limit: int = 50, offset: int = 0) -> list[Run]:
    q = select(Run)
    if status:
        q = q.where(Run.status == status)
    if repo_id is not None:
        q = q.where(Run.repo_id == repo_id)
    q = q.order_by(Run.created_at.desc()).limit(limit).offset(offset)
    return list(session.execute(q).scalars().all())


def resolve_repo_for_run(session, *, repo_id: int | None, repo_path: str | None) -> Repo:
    """`repo_id` wins if both are given. Raises RepoNotFound (unregistered)
    or RepoNotReady (registered but index not built yet)."""
    if repo_id is not None:
        repo = session.get(Repo, repo_id)
    elif repo_path:
        from pathlib import Path
        repo = session.execute(
            select(Repo).where(Repo.root_path == str(Path(repo_path).resolve()))
        ).scalar_one_or_none()
    else:
        raise InvalidRepoPath("must supply repo_path or repo_id")
    if repo is None:
        raise RepoNotFound(f"no repo registered at {repo_path or repo_id!r} — register it via POST /v1/repos first")
    if repo.status != "ready":
        raise RepoNotReady(f"repo {repo.id} is not ready (status={repo.status}); index it first")
    return repo


# ---------------------------------------------------------------- artifacts


def _next_version(session, run_id, kind: str) -> int:
    latest = latest_artifact(session, run_id, kind)
    return (latest.version + 1) if latest else 1


def persist_artifact(session, run_id, kind: str, body: dict, version: int | None = None) -> int:
    v = version if version is not None else _next_version(session, run_id, kind)
    session.add(RunArtifact(run_id=run_id, kind=kind, version=v, body=body))
    session.commit()
    return v


def latest_artifact(session, run_id, kind: str) -> RunArtifact | None:
    return session.execute(
        select(RunArtifact)
        .where(RunArtifact.run_id == run_id, RunArtifact.kind == kind)
        .order_by(RunArtifact.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def artifact_at(session, run_id, kind: str, version: int) -> RunArtifact | None:
    return session.execute(
        select(RunArtifact).where(
            RunArtifact.run_id == run_id, RunArtifact.kind == kind, RunArtifact.version == version,
        )
    ).scalar_one_or_none()


def _validation_body(result) -> dict:
    """One shape for both gates' validation artifacts (SpecResult at gate 1,
    PlanResult at gate 2) — what the UI's renderValidation already expects."""
    return {
        "passed": result.report.passed,
        "checks": [c.model_dump() for c in result.report.checks],
        "escalation": result.escalation,
        "repair_attempts": result.repair_attempts,
    }


def _spec_result(output: SpecOutput, report: ValidationReport) -> SpecResult:
    """A SpecResult for the paths that validate outside `spec_pipeline` (a
    hand edit, the answers regen) so they persist the identical body shape."""
    return SpecResult(output=output, report=report,
                      escalation=_escalation_message(escalation_for_spec(output.spec)))


def _persist_spec(session, run: Run, result: SpecResult) -> int:
    """spec + spec_validation always share a version number."""
    v = persist_artifact(session, run.id, "spec", result.output.model_dump(mode="json"))
    persist_artifact(session, run.id, "spec_validation", _validation_body(result), version=v)
    return v


def _persist_plan(session, run: Run, result) -> int:
    """spec_plan + validation always share a version number. `result.output`
    only ever comes out of `planning._assemble` — never hand-built here."""
    v = persist_artifact(session, run.id, "spec_plan", result.output.model_dump(mode="json"))
    persist_artifact(session, run.id, "validation", _validation_body(result), version=v)
    return v


def _load_pack(session, run: Run) -> ContextPack:
    return ContextPack.model_validate(latest_artifact(session, run.id, "context_pack").body)


# ------------------------------------------------------------------ snapshot


def build_snapshot(session, run: Run) -> dict:
    """Full state for GET /runs/{id}: run fields + latest artifact of each
    kind. Context pack is summarized (paths+tiers, not full file content) —
    both gates' artifacts are returned in full. `spec`/`spec_validation` are
    gate 1's and stay in the snapshot after gate 2 opens: the approved spec is
    what the plan was generated from.
    """
    context = latest_artifact(session, run.id, "context_pack")
    spec = latest_artifact(session, run.id, "spec")
    spec_validation = latest_artifact(session, run.id, "spec_validation")
    spec_plan = latest_artifact(session, run.id, "spec_plan")
    validation = latest_artifact(session, run.id, "validation")

    chat: list[dict] = []
    if spec_plan is not None:
        plan_chat = latest_artifact(session, run.id, "plan_chat")
        if plan_chat is not None:
            chat = [
                m for m in plan_chat.body.get("messages", [])
                if m.get("plan_version") == spec_plan.version
            ]

    context_summary = None
    if context is not None:
        context_summary = {
            "version": context.version,
            "sha": context.body.get("sha"),
            "token_count": context.body.get("token_count"),
            "files": [
                {"path": f.get("path"), "tier": f.get("tier"), "label": f.get("label")}
                for f in context.body.get("files", [])
            ],
        }

    return {
        "run_id": str(run.id),
        "repo_id": run.repo_id,
        "status": run.status,
        "error": run.error,
        "request": {
            "type": run.request_type,
            "text": run.request_text,
            "attachments": run.attachments,
        },
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "context_pack": context_summary,
        "spec": {"version": spec.version, **spec.body} if spec else None,
        "spec_validation": (
            {"version": spec_validation.version, **spec_validation.body} if spec_validation else None
        ),
        "spec_plan": {"version": spec_plan.version, **spec_plan.body} if spec_plan else None,
        "validation": {"version": validation.version, **validation.body} if validation else None,
        "chat": chat,
    }


def build_plan_view(session, run: Run) -> dict:
    """GET /runs/{id}/plan: the convenience endpoint executor agents poll —
    latest spec_plan artifact only, whatever its approval state."""
    spec_plan = latest_artifact(session, run.id, "spec_plan")
    return {
        "run_id": str(run.id),
        "status": run.status,
        "version": spec_plan.version if spec_plan else None,
        "plan": spec_plan.body if spec_plan else None,
    }


def build_task_context(session, run: Run, task_id: str) -> dict:
    """GET /runs/{id}/tasks/{task_id}/context (task 10): what an executor
    needs to start one task — the task itself plus the FULL CURRENT content
    of its files, read live from the index (not the context pack, which may
    be stale/summarized by tier). Works off the latest spec_plan artifact
    regardless of approval state, same as build_plan_view."""
    spec_plan = latest_artifact(session, run.id, "spec_plan")
    if spec_plan is None:
        raise TaskNotFound(f"run {run.id} has no plan yet")
    plan = SpecPlanOutput.model_validate(spec_plan.body)
    task = next((t for t in plan.plan.tasks if t.id == task_id), None)
    if task is None:
        raise TaskNotFound(f"run {run.id} plan has no task {task_id!r}")
    return {
        "run_id": str(run.id),
        "task": task.model_dump(),
        "files": {path: iq.get_file(run.repo_id, path) for path in task.files},
    }


# --------------------------------------------------------- approve / reject
# Two gates, same three moves each: check the state, check the version the
# human was looking at, record an Approval row, then move (or, for an edit,
# stay put and just version the artifact).


def _require_status(run: Run, expected: str) -> None:
    if run.status != expected:
        raise InvalidState(f"run {run.id} is {run.status!r}, expected {expected!r}")


def _check_version(run: Run, latest: RunArtifact | None, version: int, kind: str) -> None:
    latest_v = latest.version if latest else None
    if latest_v != version:
        raise VersionConflict(
            f"run {run.id}: latest {kind} version is {latest_v}, request had {version}"
        )


def _gate(session, run: Run, version: int, status: str, kind: str) -> RunArtifact:
    _require_status(run, status)
    latest = latest_artifact(session, run.id, kind)
    _check_version(run, latest, version, kind)
    return latest


def _record(session, run: Run, action: str, payload: dict) -> None:
    session.add(Approval(run_id=run.id, actor="human", action=action, payload=payload))
    session.commit()


# -- gate 1: the spec ---------------------------------------------------------


def submit_spec_answers(session, run: Run, version: int, answers: list[dict]) -> dict:
    """Answers to the open questions: recorded, then back to spec_drafting for
    a regen. Caller (api.py) submits `regenerate_spec_with_answers` to the
    background pool — this function only does the synchronous half."""
    _gate(session, run, version, "awaiting_spec_approval", "spec")
    payload = {"answers": answers}
    _record(session, run, "spec_answer", payload)
    transition(session, run, "spec_drafting", event_type="spec_answers_submitted", payload=payload)
    return {"status": "spec_drafting"}


def apply_spec_edits(session, run: Run, version: int, spec: Spec) -> dict:
    """A hand edit of the spec: deterministic, no LLM, no state change. The
    submitted spec must pass `validate_spec` — no override — and the run's open
    questions carry over untouched (they are answered via the answers path).

    Not a `transition()`: awaiting_spec_approval -> awaiting_spec_approval is a
    self-transition, which the state machine rejects on purpose. The Event is
    written directly so the edit still shows up in the audit log / poll stream.
    """
    latest = _gate(session, run, version, "awaiting_spec_approval", "spec")
    previous = SpecOutput.model_validate(latest.body)
    edited = SpecOutput(spec=spec, open_questions=previous.open_questions)

    report = validate_spec(edited, run.repo_id)
    if not report.passed:
        raise SpecInvalid(f"run {run.id}: edited spec failed validation", report)

    new_version = _persist_spec(session, run, _spec_result(edited, report))
    _record(session, run, "spec_edit", {"version": new_version})
    session.add(Event(run_id=run.id, type="spec_edited", payload={"version": new_version}))
    run.updated_at = _now()
    session.commit()
    return {"status": run.status, "version": new_version}


def approve_spec(session, run: Run, version: int) -> dict:
    """Gate 1's approve: every open question still standing binds to its
    recorded default, right in the approval payload — the audit trail shows
    exactly what was assumed before any task breakdown existed.

    ponytail: "unanswered" == still listed as an open question on the approved
    spec. Answering a question regenerates the spec, so an answered question
    only survives here if the model deliberately re-asked it.
    """
    latest = _gate(session, run, version, "awaiting_spec_approval", "spec")
    spec_output = SpecOutput.model_validate(latest.body)
    bound = [
        {"q_index": i, "question": q.q, "bound_default": q.default}
        for i, q in enumerate(spec_output.open_questions)
    ]
    payload = {"bound_defaults": bound}
    _record(session, run, "spec_approve", payload)
    transition(session, run, "planning", event_type="spec_approved", payload=payload)
    return {"status": "planning", "version": version, "bound_defaults": bound}


def reject_spec(session, run: Run, version: int, feedback: str) -> dict:
    _gate(session, run, version, "awaiting_spec_approval", "spec")
    payload = {"feedback": feedback}
    _record(session, run, "spec_reject", payload)
    transition(session, run, "spec_drafting", event_type="spec_rejected", payload=payload)
    return {"status": "spec_drafting"}


# -- gate 2: the plan ---------------------------------------------------------


def approve_plan(session, run: Run, version: int) -> dict:
    """Gate 2's approve is a bare transition: the open questions were bound to
    their defaults at gate 1, before the plan was generated from them."""
    _gate(session, run, version, "awaiting_approval", "spec_plan")
    payload = {"version": version}
    _record(session, run, "approve", payload)
    transition(session, run, "approved", event_type="approved", payload=payload)
    return {"status": "approved", "version": version}


def reject_plan(session, run: Run, version: int, feedback: str) -> dict:
    """Rejecting the plan regenerates the PLAN — the spec stays approved."""
    _gate(session, run, version, "awaiting_approval", "spec_plan")
    payload = {"feedback": feedback}
    _record(session, run, "reject", payload)
    transition(session, run, "planning", event_type="rejected", payload=payload)
    return {"status": "planning"}


# ponytail: synchronous ~15s LLM call in the request thread; move to EXECUTOR
# + poll if it ever times out.
def chat_turn(session, run: Run, version: int, message: str,
              *, gateway_factory=LLMGateway) -> dict:
    """Gate 2's prose Q&A: never mutates the plan, just explains it. History is
    scoped to `version` — a new plan version starts a fresh turn budget, old
    turns from a superseded version don't count or get replayed to the model."""
    latest_plan = _gate(session, run, version, "awaiting_approval", "spec_plan")
    spec_plan = SpecPlanOutput.model_validate(latest_plan.body)
    validation = latest_artifact(session, run.id, "validation")
    validation_body = validation.body if validation else {}

    chat_artifact = latest_artifact(session, run.id, "plan_chat")
    all_messages = list(chat_artifact.body.get("messages", [])) if chat_artifact else []
    # ponytail: whole history replayed, capped at MAX_CHAT_TURNS (~14k tokens
    # worst case). Summarize oldest turns if a longer conversation is ever
    # needed.
    history = [m for m in all_messages if m["plan_version"] == version]

    cap = get_settings().max_chat_turns
    turns_used = sum(1 for m in history if m["role"] == "human")
    if turns_used >= cap:
        raise ChatLimitReached(
            f"chat limit reached for plan v{version} ({cap} turns); "
            "approve, or reject with feedback to re-plan"
        )

    gateway = gateway_factory()
    answer = answer_plan_question(
        run.request_text, spec_plan, validation_body, history, message, gateway,
        run_id=str(run.id),
    )

    all_messages.append({"role": "human", "content": message, "plan_version": version, "ts": _now().isoformat()})
    all_messages.append({"role": "assistant", "content": answer, "plan_version": version, "ts": _now().isoformat()})
    persist_artifact(session, run.id, "plan_chat", {"messages": all_messages})

    turns_used += 1
    _record(session, run, "chat", {"version": version, "message": message, "answer": answer})
    session.add(Event(run_id=run.id, type="chat_turn", payload={"version": version}))
    run.updated_at = _now()
    session.commit()

    return {"answer": answer, "turns_used": turns_used, "turns_left": cap - turns_used}


def cancel(session, run: Run) -> dict:
    if run.status in TERMINAL:
        raise InvalidState(f"run {run.id} is already {run.status!r}, cannot cancel")
    transition(session, run, "cancelled", event_type="cancelled")
    return {"status": "cancelled"}


# --------------------------------------------------------- idempotency store


def idempotent_replay(session, key: str, endpoint: str) -> tuple[int, dict] | None:
    row = session.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint)
    ).scalar_one_or_none()
    return None if row is None else (row.status_code, row.response_body)


def idempotent_save(session, key: str, endpoint: str, status_code: int, body: dict) -> None:
    session.add(IdempotencyKey(key=key, endpoint=endpoint, status_code=status_code, response_body=body))
    try:
        session.commit()
    except IntegrityError:
        # ponytail: a concurrent request with the same key lost the race
        # between our replay-check and this insert. Single-team internal
        # tool, no real concurrent-retry storms expected — if that changes,
        # add a row-level lock around the check+insert.
        session.rollback()


# ------------------------------------------------------- background jobs ---
# Every job below is what api.py submits to its ThreadPoolExecutor. Each
# opens its own session (background-thread work must not share a session
# with the request thread) and never lets an exception escape uncaught —
# a run must always end up in a terminal-for-now state, never silently stuck.


def _fail(session, run: Run, exc: Exception) -> None:
    run.error = str(exc)[:4000]
    if "failed" in ALLOWED_TRANSITIONS.get(run.status, set()):
        transition(session, run, "failed", event_type="error", payload={"error": run.error})
    else:
        session.commit()


def _job(run_id, work, gateway_factory) -> None:
    """The scaffolding every job below shares: its own session, its own
    gateway, and a run that always lands somewhere — never silently stuck."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None:
            return
        try:
            work(session, run, gateway_factory())
        except Exception as exc:  # noqa: BLE001 - a run must never hang silently
            run = session.get(Run, run_id)
            if run is not None:
                _fail(session, run, exc)
    finally:
        session.close()


def _with_feedback(run: Run, feedback: str, what: str) -> str:
    return (
        f"{run.request_text}\n\n"
        f"FEEDBACK FROM REJECTED {what} (address this in the new {what.lower()}):\n{feedback}"
    )


def execute_pipeline(run_id, *, gateway_factory=LLMGateway) -> None:
    """Gate 1's pipeline: pending was already set by create_run(); this drives
    context_building -> spec_drafting -> awaiting_spec_approval (or failed).
    It generates NO plan — that waits for a human to approve the scope.

    Stage A runs as its own call (not folded into spec_pipeline) so the
    context_pack artifact is persisted while the run is genuinely in
    context_building — spec_pipeline then takes the same pack via its `pack=`
    kwarg and skips re-deriving it.
    """
    def work(session, run, gateway):
        transition(session, run, "context_building")
        pack = build_context(run.repo_id, run.request_text, run.attachments or [], gateway=gateway)
        persist_artifact(session, run.id, "context_pack", pack.model_dump(mode="json"), version=1)

        transition(session, run, "spec_drafting")
        result = spec_pipeline(run.request_text, run.repo_id, gateway, pack=pack, run_id=str(run.id))
        _persist_spec(session, run, result)

        transition(session, run, "awaiting_spec_approval")

    _job(run_id, work, gateway_factory)


def generate_plan_job(run_id, *, gateway_factory=LLMGateway) -> None:
    """Gate 2's pipeline, kicked by `approve_spec`: the approved spec plus the
    stored pack go into plan_pipeline, which generates the task DAG, assembles
    it onto that spec verbatim and runs the full 7 checks. planning ->
    awaiting_approval (or failed)."""
    def work(session, run, gateway):
        spec = SpecOutput.model_validate(latest_artifact(session, run.id, "spec").body)
        pack = _load_pack(session, run)
        result = plan_pipeline(run.request_text, run.repo_id, spec, gateway,
                               pack=pack, run_id=str(run.id))
        _persist_plan(session, run, result)

        transition(session, run, "awaiting_approval")

    _job(run_id, work, gateway_factory)


def regenerate_spec_with_answers(run_id, answers: list[dict], base_version: int,
                                  *, gateway_factory=LLMGateway) -> None:
    """Gate 1's answers path: one repair-style call (not a full spec_pipeline
    repair loop) that hands the model its own previous spec plus the user's
    answers — each paired with the question it answers, by q_index — and asks
    for the corrected JSON, then validates once."""
    def work(session, run, gateway):
        previous = SpecOutput.model_validate(artifact_at(session, run.id, "spec", base_version).body)
        pack = _load_pack(session, run)

        answers_block = "\n".join(
            f"- Q: {previous.open_questions[a['q_index']].q}\n  A: {a['answer']}"
            for a in answers
            if 0 <= a["q_index"] < len(previous.open_questions)
        ) or "(no answers given)"
        user = (
            f"{render_user_prompt(run.request_text, pack)}\n\n"
            f"YOUR PREVIOUS RESPONSE:\n```json\n{previous.model_dump_json(indent=2)}\n```\n\n"
            "The user answered the open questions:\n" + answers_block + "\n\n"
            "Incorporate these answers into the spec. Emit the full corrected JSON."
        )
        output = gateway.complete_json(
            tier="strong", system=load_prompt("spec_only"), user=user,
            schema=SpecOutput, max_tokens=4096, purpose="spec_answers", run_id=str(run.id),
        )
        _persist_spec(session, run, _spec_result(output, validate_spec(output, run.repo_id)))

        transition(session, run, "awaiting_spec_approval")

    _job(run_id, work, gateway_factory)


def regenerate_spec_full(run_id, feedback: str, base_version: int,
                          *, gateway_factory=LLMGateway) -> None:
    """Gate 1's reject path: the whole spec generate + validate +
    bounded-repair loop runs again, reusing the stored context pack (no
    partial-regen / re-retrieval machinery)."""
    def work(session, run, gateway):
        result = spec_pipeline(_with_feedback(run, feedback, "SPEC"), run.repo_id, gateway,
                               pack=_load_pack(session, run), max_repairs=1, run_id=str(run.id))
        _persist_spec(session, run, result)

        transition(session, run, "awaiting_spec_approval")

    _job(run_id, work, gateway_factory)


def regenerate_plan_full(run_id, feedback: str, base_version: int,
                         *, gateway_factory=LLMGateway) -> None:
    """Gate 2's reject path: only the PLAN is regenerated. The approved spec is
    passed straight back into plan_pipeline and copied through untouched — a
    rejected plan never rewrites a spec a human already signed off on."""
    def work(session, run, gateway):
        spec = SpecOutput.model_validate(latest_artifact(session, run.id, "spec").body)
        result = plan_pipeline(_with_feedback(run, feedback, "PLAN"), run.repo_id, spec, gateway,
                               pack=_load_pack(session, run), max_repairs=1, run_id=str(run.id))
        _persist_plan(session, run, result)

        transition(session, run, "awaiting_approval")

    _job(run_id, work, gateway_factory)
