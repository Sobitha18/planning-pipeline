"""ORM schema for the repo index: repos / files / symbols.

Plain SQLAlchemy declarative models. No business logic here — that lives in
indexer/full_index.py (writes) and index_query.py (reads).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Computed, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    indexed_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="empty")

    files: Mapped[list["File"]] = relationship(cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("repo_id", "path", name="uq_files_repo_path"),
        Index("files_repo_id_idx", "repo_id"),
        Index("files_content_tsv_idx", "content_tsv", postgresql_using="gin"),
        Index(
            "files_content_trgm_idx",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    loc: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=True
    )

    symbols: Mapped[list["Symbol"]] = relationship(cascade="all, delete-orphan")


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index("symbols_name_idx", "name"),
        Index("symbols_qualified_name_idx", "qualified_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)
    exported: Mapped[bool] = mapped_column(nullable=False)


# --- Symbol graph edges (task 03) -------------------------------------------
# Derived by src/indexer/edges.py after full indexing. Full rebuild per run
# (build_edges deletes+reinserts a repo's rows) — no incremental updates yet.


class SymbolEdge(Base):
    __tablename__ = "symbol_edges"
    __table_args__ = (
        UniqueConstraint("from_symbol", "to_symbol", "kind", name="uq_symbol_edges_from_to_kind"),
        Index("symbol_edges_repo_from_idx", "repo_id", "from_symbol"),
        Index("symbol_edges_repo_to_idx", "repo_id", "to_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), nullable=False)
    from_symbol: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    to_symbol: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)          # imports|calls|implements|inherits
    confidence: Mapped[str] = mapped_column(Text, nullable=False)    # resolved|heuristic


class FileImport(Base):
    __tablename__ = "file_imports"
    __table_args__ = (Index("file_imports_file_id_idx", "file_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    # Nullable despite the task-03 spec's own "NOT NULL" annotation on this
    # column: the same spec's comment on it ("or NULL if external") and its
    # own assertion 2 ("bare zod import -> imported_path NULL") require NULL
    # for unresolved/external specifiers. Comment wins; see final report.
    imported_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_specifier: Mapped[str] = mapped_column(Text, nullable=False)
    names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)


# --- LLM gateway (task 04) -------------------------------------------------
# Call log for every request made through src.llm.LLMGateway. run_id is text
# (str(runs.id), a uuid) and unconstrained (no FK) — the gateway is called
# from background threads that log best-effort; a hard FK would let a
# logging failure corrupt a real run. Wired by task 08's runs.py, which
# passes run_id=str(run.id) through **kwargs on every pipeline call.


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("llm_calls_run_id_idx", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    ok: Mapped[bool] = mapped_column(nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- Pydantic models for LLM-produced artifacts (grown in tasks 05-06) ----
# Stage A / Stage B artifact schemas. complete_json() takes a caller-supplied
# `schema: type[BaseModel]` — these are what Stage A hands it.

from typing import Literal  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402


class ExtractedEntities(BaseModel):
    """Output of the cheap entity-extraction call over the request text."""

    symbols: list[str] = Field(default_factory=list)
    error_strings: list[str] = Field(default_factory=list)
    files_mentioned: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    request_type: Literal["bug", "feature"] = "feature"


class StackFrame(BaseModel):
    path: str
    line: int | None = None


class RerankLabel(BaseModel):
    path: str
    label: Literal["critical", "relevant", "peripheral", "irrelevant"]
    reason: str = ""


class RerankResponse(BaseModel):
    files: list[RerankLabel] = Field(default_factory=list)


class PackedFile(BaseModel):
    path: str
    tier: Literal[1, 2, 3]  # 1=full, 2=signatures+docstrings, 3=path+one-liner
    label: Literal["critical", "relevant", "peripheral"]
    reason: str
    content: str  # tier-appropriate content actually included


class ContextPack(BaseModel):
    repo_id: int
    sha: str | None = None
    request_type: str = "feature"
    files: list[PackedFile] = Field(default_factory=list)
    seed_symbols: list[str] = Field(default_factory=list)  # qualified names
    recent_commits: list[dict] = Field(default_factory=list)
    token_count: int = 0
    stats: dict = Field(default_factory=dict)  # per-step counts, for tuning


# --- Stage B artifacts (task 06) -------------------------------------------
# One strong-LLM call produces SpecPlanOutput. Only the counts the design
# states as hard limits are enforced here (criteria 1-8, questions <=3); the
# task-count threshold is an escalation flag, not a schema error (task 07).


class Criterion(BaseModel):
    id: str                       # "AC1"
    given: str
    when: str
    then: str
    verify_by: Literal["unit", "integration", "manual"]


class Assumption(BaseModel):
    text: str
    confidence: Literal["high", "low"]


class OpenQuestion(BaseModel):
    q: str
    default: str                  # binding assumption if the human skips it


class Spec(BaseModel):
    problem_statement: str
    criteria: list[Criterion] = Field(min_length=1, max_length=8)
    touchable_files: list[str]    # repo-relative paths
    non_goals: list[str]
    assumptions: list[Assumption]


class Task(BaseModel):
    id: str                       # "t1"
    title: str
    description: str              # 2-4 sentences: what to do, how it's done
    files: list[str]
    done_criteria: str
    est_size: Literal["S", "M", "L"]
    criterion_refs: list[str]


class TaskEdge(BaseModel):
    from_task: str
    to_task: str
    kind: Literal["data", "code", "contract"]


class Plan(BaseModel):
    tasks: list[Task] = Field(min_length=1)
    edges: list[TaskEdge] = Field(default_factory=list)


class SpecOutput(BaseModel):
    """Gate 1's artifact: the spec before any task breakdown exists.
    `Plan.tasks` has min_length=1, so SpecPlanOutput cannot represent
    "spec approved, plan not generated yet" — this can."""

    spec: Spec
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=3)


class SpecPlanOutput(BaseModel):
    spec: Spec
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=3)
    plan: Plan


# --- Validation artifacts (task 07) ----------------------------------------
# Produced by src/planning.py's validate(). `details` are fed back to the model
# verbatim in the repair prompt, so each one must be actionable on its own.


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    details: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    passed: bool
    checks: list[ValidationCheck] = Field(default_factory=list)


class SpecResult(BaseModel):
    """spec_pipeline's result — PlanResult's shape for gate 1. No `pack`: the
    caller that has a pack passes it in, and gate 2 needs the same one."""

    output: SpecOutput
    report: ValidationReport
    escalation: str | None = None
    repair_attempts: int = 0


class PlanResult(BaseModel):
    output: SpecPlanOutput
    report: ValidationReport
    escalation: str | None = None     # set when the size thresholds trip
    repair_attempts: int = 0
    # The Context Pack the pipeline used (caller-supplied via plan_pipeline's
    # pack= kwarg, or built internally when omitted). Echoed back so task 08
    # can persist it into context_packs without a second Stage A call.
    pack: ContextPack | None = None


# --- Runs (task 08) ----------------------------------------------------------
# Persisted, resumable execution of plan_pipeline behind an HTTP surface.
# ORM tables only — state machine + all business logic lives in src/runs.py,
# matching the split index tables (here) / index logic (indexer, index_query)
# already used for the rest of this codebase. db.py stays engine+session only.


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    request_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class RunArtifact(Base):
    """One versioned artifact per (run, kind). kind: context_pack | spec |
    spec_validation | spec_plan | validation | plan_chat. spec_plan and
    validation share version numbers (a plan and its validation report are
    always regenerated together), as do spec and spec_validation.

    `spec` (SpecOutput) and `spec_validation` are gate 1's artifacts;
    `spec_plan` (assembled from the approved `spec` plus the generated plan)
    and `validation` are gate 2's; `plan_chat` holds gate 2's prose Q&A.
    Written by src/runs.py."""

    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "version", name="uq_run_artifacts_run_kind_version"),
        Index("run_artifacts_run_id_idx", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class Approval(Base):
    """One row per human action on a run. action: gate 1 — spec_edit,
    spec_answer, spec_approve, spec_reject; gate 2 — approve, reject, chat.
    (`answer` is gate 1's `spec_answer` now: open questions are answered
    before the plan is generated from them.)"""

    __tablename__ = "approvals"
    __table_args__ = (Index("approvals_run_id_idx", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False, default="human")
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class Event(Base):
    """Append-only, replayable log of everything that happened to a run.
    `seq` is a global serial (not per-run) — plenty for `?after=` polling
    ordering, and avoids a second index just to hand out per-run sequence
    numbers."""

    __tablename__ = "events"
    __table_args__ = (Index("events_run_id_seq_idx", "run_id", "seq"),)

    seq: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class IdempotencyKey(Base):
    """POST replay cache: (Idempotency-Key, endpoint) -> the response first
    returned for it. `endpoint` is part of the identity so a client that
    accidentally reuses a key on a different route doesn't collide."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", "endpoint", name="uq_idempotency_keys_key_endpoint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )


# --- Multi-repo: projects + repo router -------------------------------------
# A project is a named set of already-registered repos. The repo router
# (src/repo_router.py) picks which of a project's repos a request touches.


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    repos: Mapped[list["ProjectRepo"]] = relationship(cascade="all, delete-orphan")


class ProjectRepo(Base):
    __tablename__ = "project_repos"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )


class RepoEvidence(BaseModel):
    path: str = Field(description="Indexed file path inside the repo that supports the choice")
    note: str = Field(description="What in that file connects it to the request")


class AgentEvidence(BaseModel):
    """What the router agent writes: it cites a file by the number a tool
    result showed for it, never by typing a path. The code resolves the number
    to the real path (src/repo_router.EvidenceRegistry)."""

    file: str = Field(description="File number exactly as shown in tool results, e.g. F12")
    note: str = Field(description="What in that file connects it to the request")


class AgentRepo(BaseModel):
    repo: str = Field(description="Repo name exactly as list_repos returned it")
    role: Literal["primary", "impacted"]
    reason: str
    evidence: list[AgentEvidence] = Field(min_length=1)


class AgentRuledOut(BaseModel):
    repo: str = Field(description="Repo name exactly as list_repos returned it")
    reason: str = Field(description="Why this repo is not touched, citing the search or file that cleared it")


class AgentAnswer(BaseModel):
    """The router agent's final answer. Every repo of the project must appear
    exactly once: in `repos` (primary or impacted) or in `ruled_out`. Code
    checks this, so a repo can't drop out of the answer silently."""

    repos: list[AgentRepo]
    ruled_out: list[AgentRuledOut] = Field(default_factory=list)


class SelectedRepo(BaseModel):
    repo: str
    role: Literal["primary", "impacted"]
    reason: str
    evidence: list[RepoEvidence] = Field(min_length=1)


class ResolvedRepo(SelectedRepo):
    repo_id: int


class RuledOutRepo(BaseModel):
    repo: str
    repo_id: int
    reason: str


class RepoSelection(BaseModel):
    project_id: int
    repos: list[ResolvedRepo]
    ruled_out: list[RuledOutRepo] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
