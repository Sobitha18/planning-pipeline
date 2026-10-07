"""FastAPI HTTP surface over src.runs. Thin by design: parse request, call a
src.runs function, map its result/exception to an HTTP response. All business
logic (state machine, versioning, validation) lives in src.runs; all indexing
logic lives in src.indexer. This module owns exactly two things runs.py
shouldn't: the background thread pool, and HTTP <-> domain-exception mapping.

Out of scope (task-prompts/08-runs-api.md): auth, SSE, webapp, rate limiting,
async DB, a real queue, partial regeneration.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from src import runs
from src.db import get_session, init_schema
from src.indexer.edges import build_edges
from src.indexer.full_index import index_repo
from src.models import Event, File, Repo, Spec, Symbol

EXECUTOR = ThreadPoolExecutor(max_workers=4)
WEBAPP_INDEX = Path(__file__).resolve().parent.parent / "webapp" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()  # create_all()-style, skips tables/extension that already exist
    runs.recover_interrupted_runs()
    yield


app = FastAPI(lifespan=lifespan)


# ------------------------------------------------------------------- webapp
# Task 09: the approval UI is a single static file, no templating, no private
# endpoints — it's just another client of the /v1 API below.


@app.get("/", include_in_schema=False)
def serve_webapp():
    return FileResponse(WEBAPP_INDEX)


# ------------------------------------------------------------- problem+json


def _problem(status: int, title: str, detail: str, **extensions) -> JSONResponse:
    """RFC 7807. `extensions` are extra top-level members (7807 §3.2) — used
    for SpecInvalid's `checks`, which the UI renders with the same
    renderValidation it uses for a validation artifact."""
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status,
                 "detail": detail, **extensions},
        media_type="application/problem+json",
    )


_DOMAIN_STATUS = {
    runs.RunNotFound: (404, "Run Not Found"),
    runs.RepoNotFound: (404, "Repo Not Found"),
    runs.InvalidRepoPath: (422, "Invalid Repo Path"),
    runs.RepoNotReady: (409, "Repo Not Ready"),
    runs.IllegalTransition: (409, "Illegal State Transition"),
    runs.VersionConflict: (409, "Version Conflict"),
    runs.InvalidState: (409, "Invalid State"),
    runs.TaskNotFound: (404, "Task Not Found"),
    runs.SpecInvalid: (422, "Spec Invalid"),
    runs.ChatLimitReached: (409, "Chat Limit Reached"),
}


@app.exception_handler(runs.DomainError)
async def handle_domain_error(request, exc: runs.DomainError):
    extensions = (
        {"checks": [c.model_dump() for c in exc.report.checks]}
        if isinstance(exc, runs.SpecInvalid) else {}
    )
    for exc_type, (status, title) in _DOMAIN_STATUS.items():
        if isinstance(exc, exc_type):
            return _problem(status, title, str(exc), **extensions)
    return _problem(500, "Internal Error", str(exc))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request, exc: RequestValidationError):
    return _problem(422, "Validation Error", str(exc.errors()))


# ------------------------------------------------------------------ request bodies


class RequestPayload(BaseModel):
    type: str = "feature"
    text: str
    attachments: list[str] = Field(default_factory=list)


class CreateRunBody(BaseModel):
    repo_path: str | None = None
    repo_id: int | None = None
    request: RequestPayload


class AnswerItem(BaseModel):
    q_index: int
    answer: str


class ApproveBody(BaseModel):
    version: int


class SpecApproveBody(BaseModel):
    """Gate 1: answers present -> regenerate the spec with them; absent ->
    approve the scope and start planning. One route, no ordering rules."""

    version: int
    answers: list[AnswerItem] | None = None


class SpecEditBody(BaseModel):
    """`spec` is a full `Spec`, not a patch (one client, no jsonpatch dep) and
    typed, not a dict — a malformed edit is then a plain 422 from the
    request-validation handler instead of a 500 out of the domain layer.
    `open_questions` are not editable here; they carry over."""

    version: int
    spec: Spec


class RejectBody(BaseModel):
    version: int
    feedback: str


class ChatBody(BaseModel):
    version: int
    message: str


class RegisterRepoBody(BaseModel):
    root_path: str


# -------------------------------------------------------------- idempotency
# Every POST goes through this pair: check-and-replay before doing any work,
# save-after on the way out. `endpoint` disambiguates a key reused (by
# mistake) across different routes.


def _replay_or(session, key: str | None, endpoint: str):
    if key is None:
        return None
    hit = runs.idempotent_replay(session, key, endpoint)
    if hit is None:
        return None
    status_code, body = hit
    return JSONResponse(status_code=status_code, content=body)


def _save(session, key: str | None, endpoint: str, status_code: int, body: dict) -> JSONResponse:
    if key is not None:
        runs.idempotent_save(session, key, endpoint, status_code, body)
    return JSONResponse(status_code=status_code, content=body)


# ------------------------------------------------------------------- repos


def _index_repo_job(repo_id: int) -> None:
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            return
        try:
            index_repo(repo.root_path)   # manages its own session; sets ready/failed itself
            build_edges(repo_id)
        except Exception:  # noqa: BLE001 - index_repo already recorded status=failed
            pass
    finally:
        session.close()


@app.post("/v1/repos", status_code=201)
def register_repo(body: RegisterRepoBody, idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = "POST /v1/repos"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        root = Path(body.root_path)
        if not root.exists() or not root.is_dir():
            return _problem(422, "Invalid Repo Path", f"{body.root_path!r} does not exist or is not a directory")
        resolved = str(root.resolve())

        repo = session.execute(select(Repo).where(Repo.root_path == resolved)).scalar_one_or_none()
        if repo is None:
            repo = Repo(root_path=resolved, status="building")
            session.add(repo)
        else:
            repo.status = "building"
        session.commit()

        EXECUTOR.submit(_index_repo_job, repo.id)

        payload = {"repo_id": repo.id, "status": repo.status}
        if not (root / ".git").exists():
            payload["warning"] = "not a git repo; indexing without SHA pinning"
        return _save(session, idempotency_key, endpoint, 201, payload)
    finally:
        session.close()


@app.get("/v1/repos")
def list_repos():
    session = get_session()
    try:
        rows = session.execute(select(Repo)).scalars().all()
        out = []
        for r in rows:
            file_count = session.execute(
                select(func.count()).select_from(File).where(File.repo_id == r.id)
            ).scalar()
            symbol_count = session.execute(
                select(func.count()).select_from(Symbol)
                .join(File, Symbol.file_id == File.id)
                .where(File.repo_id == r.id)
            ).scalar()
            out.append({
                "repo_id": r.id, "root_path": r.root_path, "status": r.status,
                "indexed_sha": r.indexed_sha,
                "indexed_at": r.indexed_at.isoformat() if r.indexed_at else None,
                "file_count": file_count, "symbol_count": symbol_count,
            })
        return out
    finally:
        session.close()


@app.post("/v1/repos/{repo_id}/reindex", status_code=202)
def reindex_repo(repo_id: int, idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = f"POST /v1/repos/{repo_id}/reindex"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        repo = session.get(Repo, repo_id)
        if repo is None:
            return _problem(404, "Repo Not Found", f"no repo with id {repo_id}")
        repo.status = "building"
        session.commit()

        EXECUTOR.submit(_index_repo_job, repo_id)

        return _save(session, idempotency_key, endpoint, 202, {"status": "building"})
    finally:
        session.close()


# -------------------------------------------------------------------- runs


@app.post("/v1/runs", status_code=201)
def create_run(body: CreateRunBody, idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = "POST /v1/runs"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        repo = runs.resolve_repo_for_run(session, repo_id=body.repo_id, repo_path=body.repo_path)
        run = runs.create_run(session, repo.id, body.request.type, body.request.text, body.request.attachments)

        EXECUTOR.submit(runs.execute_pipeline, run.id)

        payload = {"run_id": str(run.id), "status": run.status, "sha": repo.indexed_sha}
        return _save(session, idempotency_key, endpoint, 201, payload)
    finally:
        session.close()


@app.get("/v1/runs")
def list_runs_endpoint(status: str | None = None, repo_id: int | None = None,
                        limit: int = 50, offset: int = 0):
    session = get_session()
    try:
        rows = runs.list_runs(session, status=status, repo_id=repo_id, limit=limit, offset=offset)
        return [
            {"run_id": str(r.id), "repo_id": r.repo_id, "status": r.status,
             "request_text": r.request_text,
             "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()}
            for r in rows
        ]
    finally:
        session.close()


@app.get("/v1/runs/{run_id}")
def get_run_endpoint(run_id: uuid.UUID):
    session = get_session()
    try:
        run = runs.get_run(session, run_id)
        return runs.build_snapshot(session, run)
    finally:
        session.close()


@app.get("/v1/runs/{run_id}/plan")
def get_plan_endpoint(run_id: uuid.UUID):
    session = get_session()
    try:
        run = runs.get_run(session, run_id)
        return runs.build_plan_view(session, run)
    finally:
        session.close()


@app.get("/v1/runs/{run_id}/tasks/{task_id}/context")
def get_task_context_endpoint(run_id: uuid.UUID, task_id: str):
    session = get_session()
    try:
        run = runs.get_run(session, run_id)
        return runs.build_task_context(session, run, task_id)
    finally:
        session.close()


@app.get("/v1/runs/{run_id}/events")
def get_events_endpoint(run_id: uuid.UUID, after: int | None = None):
    session = get_session()
    try:
        run = runs.get_run(session, run_id)  # 404s a bad run id before we return []
        q = select(Event).where(Event.run_id == run.id)
        if after is not None:
            q = q.where(Event.seq > after)
        q = q.order_by(Event.seq)
        rows = session.execute(q).scalars().all()
        return [
            {"seq": e.seq, "type": e.type, "payload": e.payload, "ts": e.ts.isoformat()}
            for e in rows
        ]
    finally:
        session.close()


# ------------------------------------------------------------------- gate 1
# Three single-purpose routes over the spec. No combination or ordering logic:
# each one does exactly what its name says to the version the human was
# looking at.


@app.post("/v1/runs/{run_id}/spec")
def edit_spec_endpoint(run_id: uuid.UUID, body: SpecEditBody,
                        idempotency_key: str = Header(alias="Idempotency-Key")):
    """Deterministic hand edit: no LLM, no state change, spec v+1. A spec that
    fails validate_spec comes back as 422 with the failing `checks`."""
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/spec"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.apply_spec_edits(session, run, body.version, body.spec)

        return _save(session, idempotency_key, endpoint, 200, result)
    finally:
        session.close()


@app.post("/v1/runs/{run_id}/spec/approve", status_code=202)
def approve_spec_endpoint(run_id: uuid.UUID, body: SpecApproveBody,
                           idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/spec/approve"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        if body.answers:
            answers = [a.model_dump() for a in body.answers]
            result = runs.submit_spec_answers(session, run, body.version, answers)
            EXECUTOR.submit(runs.regenerate_spec_with_answers, run.id, answers, body.version)
        else:
            result = runs.approve_spec(session, run, body.version)
            EXECUTOR.submit(runs.generate_plan_job, run.id)

        return _save(session, idempotency_key, endpoint, 202, result)
    finally:
        session.close()


@app.post("/v1/runs/{run_id}/spec/reject", status_code=202)
def reject_spec_endpoint(run_id: uuid.UUID, body: RejectBody,
                          idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/spec/reject"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.reject_spec(session, run, body.version, body.feedback)
        EXECUTOR.submit(runs.regenerate_spec_full, run.id, body.feedback, body.version)

        return _save(session, idempotency_key, endpoint, 202, result)
    finally:
        session.close()


# ------------------------------------------------------------------- gate 2


@app.post("/v1/runs/{run_id}/approve")
def approve_endpoint(run_id: uuid.UUID, body: ApproveBody, idempotency_key: str = Header(alias="Idempotency-Key")):
    """Final approval of the plan. No `answers` overload — open questions are
    answered and bound at gate 1, before the plan exists."""
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/approve"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.approve_plan(session, run, body.version)

        return _save(session, idempotency_key, endpoint, 200, result)
    finally:
        session.close()


@app.post("/v1/runs/{run_id}/reject", status_code=202)
def reject_endpoint(run_id: uuid.UUID, body: RejectBody, idempotency_key: str = Header(alias="Idempotency-Key")):
    """Rejecting the plan regenerates the plan from the still-approved spec."""
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/reject"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.reject_plan(session, run, body.version, body.feedback)
        EXECUTOR.submit(runs.regenerate_plan_full, run.id, body.feedback, body.version)

        return _save(session, idempotency_key, endpoint, 202, result)
    finally:
        session.close()


@app.post("/v1/runs/{run_id}/chat")
def chat_endpoint(run_id: uuid.UUID, body: ChatBody, idempotency_key: str = Header(alias="Idempotency-Key")):
    """Gate 2's prose Q&A: synchronous — the answer comes back in this same
    response, no polling. Idempotency-wrapped like every other POST: a
    retried key replays the same answer instead of paying for a second LLM
    call, a real cost win here."""
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/chat"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.chat_turn(session, run, body.version, body.message)

        return _save(session, idempotency_key, endpoint, 200, result)
    finally:
        session.close()


@app.post("/v1/runs/{run_id}/cancel")
def cancel_endpoint(run_id: uuid.UUID, idempotency_key: str = Header(alias="Idempotency-Key")):
    session = get_session()
    try:
        endpoint = f"POST /v1/runs/{run_id}/cancel"
        replay = _replay_or(session, idempotency_key, endpoint)
        if replay is not None:
            return replay

        run = runs.get_run(session, run_id)
        result = runs.cancel(session, run)

        return _save(session, idempotency_key, endpoint, 200, result)
    finally:
        session.close()
