"""Stage B — two strong LLM calls behind two human gates: request +
ContextPack -> spec + questions (gate 1), then approved spec -> plan (gate 2)
— plus the deterministic half that keeps both honest.

Generation (`generate_spec`, `generate_plan`) only guarantees the output parsed
against the pydantic schema. Everything after it is pure code: `validate_spec()`
runs the 3 spec-only checks at gate 1, `validate()` runs all 7 against the
assembled output at gate 2, `augment_edges()` adds task dependencies the symbol
graph proves and the model missed, and `spec_pipeline()`/`plan_pipeline()` wire
generation -> validation -> a bounded repair loop, flagging oversized work
instead of pretending it is fine.

`_assemble()` is the single writer of a SpecPlanOutput from its parts: the
approved spec is copied through verbatim, never regenerated at gate 2.

LLM proposes; this module disposes.
"""

from __future__ import annotations

import re
from pathlib import Path

from src import index_query as iq
from src.config import get_settings
from src.indexer.extractor import SUPPORTED_EXTENSIONS
from src.llm import LLMGateway, load_prompt
from src.models import (
    ContextPack,
    OpenQuestion,
    Plan,
    PlanResult,
    Spec,
    SpecOutput,
    SpecPlanOutput,
    SpecResult,
    TaskEdge,
    ValidationCheck,
    ValidationReport,
)

TIER_NAMES = {1: "full source", 2: "signatures + docstrings", 3: "path only"}


def render_pack(pack: ContextPack) -> str:
    """Serialize the pack for the prompt: per file path + tier + label +
    content. Tier 3 carries no body — its `content` IS the reason, already in
    the header — so it renders as one line. Order follows the pack, which is
    rank order, so the same pack always renders to the same string.
    """
    blocks = []
    for f in pack.files:
        head = f"### {f.path}\n[tier {f.tier}: {TIER_NAMES[f.tier]}] [{f.label}] {f.reason}"
        if f.tier == 3 or not f.content.strip():
            blocks.append(head)
        else:
            blocks.append(f"{head}\n```\n{f.content}\n```")
    return "\n\n".join(blocks)


def render_user_prompt(request_text: str, pack: ContextPack) -> str:
    return f"REQUEST:\n{request_text}\n\nCONTEXT PACK ({len(pack.files)} files):\n\n{render_pack(pack)}"


def render_plan_prompt(request_text: str, pack: ContextPack, spec: SpecOutput) -> str:
    """Gate 2's user prompt: the same request + pack rendering gate 1 saw, plus
    the spec a human actually signed off on."""
    return (
        f"{render_user_prompt(request_text, pack)}\n\n"
        f"APPROVED SPEC (a human signed off on this — do not alter it):\n"
        f"```json\n{spec.model_dump_json(indent=2)}\n```"
    )


def generate_spec(request_text: str, pack: ContextPack,
                  gateway: LLMGateway | None = None, **kwargs) -> SpecOutput:
    """Gate 1's strong-tier call: spec + open questions, no tasks. `kwargs`
    passes run_id/budget through to the gateway when a run wants its calls
    logged and metered."""
    gateway = gateway or LLMGateway()
    return gateway.complete_json(
        tier="strong",
        system=load_prompt("spec_only"),
        user=render_user_prompt(request_text, pack),
        schema=SpecOutput,
        max_tokens=4096,
        purpose="spec",
        **kwargs,
    )


def generate_plan(request_text: str, pack: ContextPack, spec: SpecOutput,
                  gateway: LLMGateway | None = None, **kwargs) -> Plan:
    """Gate 2's strong-tier call: the task DAG for an already-approved spec."""
    gateway = gateway or LLMGateway()
    return gateway.complete_json(
        tier="strong",
        system=load_prompt("plan_only"),
        user=render_plan_prompt(request_text, pack, spec),
        schema=Plan,
        max_tokens=8192,
        purpose="plan",
        **kwargs,
    )


def render_chat_prompt(request_text: str, spec_plan: SpecPlanOutput,
                       validation_body: dict, history: list[dict]) -> str:
    """Gate 2's chat prompt: explains an already-written plan, so it renders
    the spec/plan/validation-report facts directly — deliberately NOT the
    context pack, which is retrieval for writing, not for explaining.
    `augment_edges` already records its own evidence in the validation
    report's `edge_augmentation` check details, which answers "why does X
    depend on Y" directly at ~200 tokens instead of the ~25k-token pack.
    """
    spec = spec_plan.spec
    criteria = "\n".join(
        f"- {c.id} ({c.verify_by}): given {c.given}, when {c.when}, then {c.then}"
        for c in spec.criteria
    ) or "(none)"
    tasks = "\n".join(
        f"- {t.id} [{t.est_size}] {t.title}: {t.description} "
        f"(files: {', '.join(t.files)}; criteria: {', '.join(t.criterion_refs)}; "
        f"done: {t.done_criteria})"
        for t in spec_plan.plan.tasks
    ) or "(none)"
    edges = "\n".join(
        f"- {e.from_task} -> {e.to_task} ({e.kind})" for e in spec_plan.plan.edges
    ) or "(none)"
    checks = "\n".join(
        f"[{c['name']}] passed={c['passed']}"
        + ("\n  " + "\n  ".join(c["details"]) if c["details"] else "")
        for c in validation_body.get("checks", [])
    ) or "(no validation report)"
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history) or "(no prior turns)"

    return (
        f"REQUEST:\n{request_text}\n\n"
        f"APPROVED SPEC:\n"
        f"problem_statement: {spec.problem_statement}\n"
        f"criteria:\n{criteria}\n"
        f"touchable_files: {', '.join(spec.touchable_files)}\n"
        f"non_goals: {', '.join(spec.non_goals)}\n\n"
        f"CURRENT PLAN:\n"
        f"tasks:\n{tasks}\n"
        f"edges:\n{edges}\n\n"
        f"VALIDATION REPORT (edge_augmentation's details are the evidence for every "
        f"dependency edge the symbol graph added):\n{checks}\n\n"
        f"PRIOR CHAT ON THIS PLAN VERSION:\n{convo}"
    )


def answer_plan_question(request_text: str, spec_plan: SpecPlanOutput, validation_body: dict,
                         history: list[dict], question: str,
                         gateway: LLMGateway | None = None, **kwargs) -> str:
    """One `complete_text` call, no schema, no repair loop — chat never
    mutates the plan, so there is nothing here to validate."""
    gateway = gateway or LLMGateway()
    return gateway.complete_text(
        tier="strong",
        system=load_prompt("plan_chat"),
        user=render_chat_prompt(request_text, spec_plan, validation_body, history)
             + "\n\nQUESTION: " + question,
        max_tokens=1024,
        purpose="plan_chat",
        **kwargs,
    )


def _assemble(spec_output: SpecOutput, plan: Plan) -> SpecPlanOutput:
    """The ONLY place a SpecPlanOutput is built from parts. The approved spec
    and its open questions are copied through verbatim — gate 2 never rewrites
    what gate 1 approved (the "no dual write" invariant)."""
    return SpecPlanOutput(
        spec=spec_output.spec,
        open_questions=spec_output.open_questions,
        plan=plan,
    )


# =========================================================== validators (07)
# Seven deterministic checks. Every `details` entry is fed back to the model
# verbatim on repair, so each one names the offending id/path and what is
# wrong with it — never a bare "invalid".


# Path-shaped token inside prose. Parens and brackets are load-bearing: a
# Next.js app dir is full of route groups and dynamic segments
# — src/app/(protected)/do/[id]/page.tsx is one path, not five words.
_PATHISH = re.compile(r"[A-Za-z0-9_(\[][A-Za-z0-9_()\[\]./-]{2,}")


class _RepoPaths:
    """One query's worth of ground truth: what the index actually contains."""

    def __init__(self, repo_id):
        self.paths = set(iq.list_paths(repo_id))
        self.dirs = {""}
        self.basenames = set()
        for p in self.paths:
            parts = p.split("/")
            self.basenames.add(parts[-1])
            for i in range(1, len(parts)):
                self.dirs.add("/".join(parts[:i]))

    def parent_of(self, path: str) -> str:
        return path.rsplit("/", 1)[0] if "/" in path else ""

    def mentions_something_real(self, text: str) -> bool:
        """Does this prose name a file, directory or filename that exists?"""
        for token in _PATHISH.findall(text):
            for candidate in (token, token.rstrip(".,;:!?'\"")):
                if candidate in self.paths or candidate in self.dirs or candidate in self.basenames:
                    return True
        return False


def _check(name: str, details: list[str]) -> ValidationCheck:
    return ValidationCheck(name=name, passed=not details, details=details)


# -- 1 ------------------------------------------------------------------------


def _path_validity(spec: Spec, repo: _RepoPaths) -> ValidationCheck:
    details: list[str] = []
    declared = list(dict.fromkeys(spec.touchable_files))

    def check_one(path: str, where: str) -> None:
        if not path or path.startswith("/") or path.startswith(".."):
            details.append(f"{where}: '{path}' is not a repo-relative path")
            return
        if path in repo.paths:
            return
        parent = repo.parent_of(path)
        if parent not in repo.dirs:
            details.append(
                f"{where}: new file '{path}' sits in directory '{parent}', which does not "
                f"exist in the repo — place it in an existing directory or pick an existing file"
            )
            return
        if Path(path).suffix not in SUPPORTED_EXTENSIONS and not iq.is_test_path(path):
            details.append(
                f"{where}: new file '{path}' has an unsupported extension "
                f"'{Path(path).suffix}' and does not follow a test-file naming convention"
            )

    # Task files are NOT re-checked here: scope_consistency already rejects any
    # task file that is not in touchable_files, and every one that IS in it was
    # checked by the loop above — a second pass could only ever restate the
    # same detail under a different prefix.
    for path in declared:
        check_one(path, "touchable_files")

    for goal in spec.non_goals:
        if not repo.mentions_something_real(goal):
            details.append(
                f"non-goal {goal!r} does not name any file or directory that exists in this "
                f"repo — rewrite it as \"Will not modify <real path> because ...\""
            )
    return _check("path_validity", details)


# -- 2 ------------------------------------------------------------------------


def _criteria_wellformed(spec: Spec) -> ValidationCheck:
    details: list[str] = []
    seen: set[str] = set()
    for c in spec.criteria:
        for field_name in ("given", "when", "then"):
            value = getattr(c, field_name).strip()
            if len(value.split()) < 3:
                details.append(
                    f"criterion {c.id}: '{field_name}' is {value!r} — needs a concrete clause "
                    f"of at least 3 words"
                )
        if c.id in seen:
            details.append(f"criterion id {c.id} is used more than once")
        seen.add(c.id)
    return _check("criteria_wellformed", details)


# -- 3 ------------------------------------------------------------------------


def _spec_scope_details(spec: Spec, open_questions: list[OpenQuestion]) -> list[str]:
    """The half of scope_consistency that needs no plan — the whole check at
    gate 1, the first half of it at gate 2."""
    details: list[str] = []
    for path in sorted(set(spec.touchable_files)):
        for goal in spec.non_goals:
            if path in goal:
                details.append(
                    f"'{path}' is in touchable_files but the non-goal {goal!r} says it will not "
                    f"be modified — drop it from one side"
                )

    if len(open_questions) > 3:  # pydantic caps this; belt and braces
        details.append(f"{len(open_questions)} open questions; at most 3 are allowed")
    for q in open_questions:
        if not q.default.strip():
            details.append(f"open question {q.q!r} has no default — every question must bind "
                           f"to a concrete default if the human skips it")
    return details


def _plan_scope_details(out: SpecPlanOutput) -> list[str]:
    """The plan-only half: every task file must be inside the approved scope."""
    details: list[str] = []
    touchable = set(out.spec.touchable_files)
    for task in out.plan.tasks:
        for path in task.files:
            if path not in touchable:
                details.append(
                    f"task {task.id} file '{path}' is not in touchable_files — add it there "
                    f"(and justify it) or remove it from the task"
                )
        if not task.files:
            details.append(f"task {task.id} touches no files")
    return details


def _scope_consistency(out: SpecPlanOutput) -> ValidationCheck:
    return _check(
        "scope_consistency",
        _spec_scope_details(out.spec, out.open_questions) + _plan_scope_details(out),
    )


# -- 4 ------------------------------------------------------------------------


def augment_edges(out: SpecPlanOutput, repo_id) -> ValidationCheck:
    """MUTATES `out.plan.edges`: if task B's files depend on symbols defined in
    task A's files and no A->B edge exists, add one.

    Only `resolved` symbol-graph edges count — a heuristic name match is not
    evidence of a real dependency, and a wrong edge corrupts execution order
    just as badly as a missing one. Never fails; it reports what it added.
    """
    tasks = out.plan.tasks
    file_sets = {t.id: set(t.files) for t in tasks}

    depends_on: dict[str, set[str]] = {}
    for task in tasks:
        symbol_ids = [
            s.id for path in file_sets[task.id] for s in iq.get_symbols_in_file(repo_id, path)
        ]
        hits = iq.neighbors(
            repo_id, symbol_ids, direction="callees", kinds=["imports", "calls"],
            min_confidence="resolved", limit=1000,
        ) if symbol_ids else []
        # File-level imports count too: a symbol edge only exists when the
        # imported name is used inside an indexed symbol's span, and plenty of
        # real code (TS test files, module-level wiring) uses its imports at
        # top level. FileImport.imported_path is non-NULL only when the
        # importer resolved to an indexed file, so this is resolved evidence.
        imported = {
            imp["imported_path"]
            for path in file_sets[task.id]
            for imp in iq.imports_of_file(repo_id, path)
            if imp["imported_path"]
        }
        depends_on[task.id] = {h.path for h in hits} | imported

    existing = {(e.from_task, e.to_task) for e in out.plan.edges}
    added: list[str] = []
    for a in tasks:
        for b in tasks:
            if a.id == b.id or (a.id, b.id) in existing:
                continue
            # a file both tasks touch proves nothing about their order
            shared = depends_on[b.id] & (file_sets[a.id] - file_sets[b.id])
            if not shared:
                continue
            out.plan.edges.append(TaskEdge(from_task=a.id, to_task=b.id, kind="code"))
            existing.add((a.id, b.id))
            added.append(
                f"added code edge {a.id} -> {b.id}: {b.id}'s files import/call symbols defined "
                f"in {', '.join(sorted(shared))}"
            )
    return ValidationCheck(name="edge_augmentation", passed=True, details=added)


# -- 5 ------------------------------------------------------------------------


def _find_cycle(nodes: list[str], adj: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, str] = {}
    path: list[str] = []

    def walk(n: str) -> list[str] | None:
        state[n] = "open"
        path.append(n)
        for m in adj.get(n, ()):
            if state.get(m) == "open":
                return path[path.index(m):] + [m]
            if m not in state:
                found = walk(m)
                if found:
                    return found
        state[n] = "done"
        path.pop()
        return None

    for n in nodes:
        if n not in state:
            found = walk(n)
            if found:
                return found
    return None


def _dag_acyclic(out: SpecPlanOutput) -> ValidationCheck:
    details: list[str] = []
    ids = [t.id for t in out.plan.tasks]
    known = set(ids)
    if len(known) != len(ids):
        details.append(f"duplicate task ids in plan.tasks: {sorted({i for i in ids if ids.count(i) > 1})}")

    adj: dict[str, list[str]] = {}
    for e in out.plan.edges:
        for end, side in ((e.from_task, "from"), (e.to_task, "to")):
            if end not in known:
                details.append(f"edge {e.from_task}->{e.to_task} names unknown task id '{end}' ({side})")
        if e.from_task in known and e.to_task in known:
            if e.from_task == e.to_task:
                details.append(f"task {e.from_task} depends on itself")
            else:
                adj.setdefault(e.from_task, []).append(e.to_task)

    cycle = _find_cycle(ids, adj)
    if cycle:
        details.append(f"dependency cycle: {' -> '.join(cycle)} — remove one of these edges")
    return _check("dag_acyclic", details)


# -- 6 ------------------------------------------------------------------------


def _traceability(out: SpecPlanOutput) -> ValidationCheck:
    details: list[str] = []
    criterion_ids = {c.id for c in out.spec.criteria}
    referenced: set[str] = set()

    for task in out.plan.tasks:
        valid = [r for r in task.criterion_refs if r in criterion_ids]
        for bad in [r for r in task.criterion_refs if r not in criterion_ids]:
            details.append(
                f"task {task.id} references criterion '{bad}', which does not exist "
                f"(criteria are: {', '.join(sorted(criterion_ids))})"
            )
        if not valid:
            details.append(
                f"task {task.id} ({task.title!r}) references no existing criterion — every task "
                f"must serve at least one, or it is out of scope"
            )
        referenced.update(valid)

    for cid in sorted(criterion_ids - referenced):
        details.append(f"criterion {cid} is not referenced by any task — no task delivers it")
    return _check("traceability", details)


# -- 7 ------------------------------------------------------------------------


def _test_coverage(out: SpecPlanOutput) -> ValidationCheck:
    details: list[str] = []

    def is_test_task(task) -> bool:
        return any(iq.is_test_path(f) for f in task.files) or "test" in (
            f"{task.title} {task.description}".lower()
        )

    for c in out.spec.criteria:
        if c.verify_by == "manual":
            continue
        covering = [
            t.id for t in out.plan.tasks if c.id in t.criterion_refs and is_test_task(t)
        ]
        if not covering:
            details.append(
                f"criterion {c.id} (verify_by={c.verify_by}) has no test-bearing task: no task "
                f"referencing {c.id} touches a test file or writes tests"
            )
    return _check("test_coverage", details)


# ----------------------------------------------------------------- entry point


def validate(output: SpecPlanOutput, pack: ContextPack, repo_id) -> ValidationReport:
    """The 7 checks, in order. Check 4 (edge augmentation) MUTATES
    `output.plan.edges` before the DAG check runs, so an augmented cycle is
    caught too.

    `pack` is accepted (and unused) so callers keep one call shape: the checks
    validate against the index at the pinned state, which is the ground truth
    the pack itself was built from.
    """
    repo = _RepoPaths(repo_id)
    checks = [
        _path_validity(output.spec, repo),
        _criteria_wellformed(output.spec),
        _scope_consistency(output),
        augment_edges(output, repo_id),
        _dag_acyclic(output),
        _traceability(output),
        _test_coverage(output),
    ]
    return ValidationReport(passed=all(c.passed for c in checks), checks=checks)


def validate_spec(spec_output: SpecOutput, repo_id) -> ValidationReport:
    """Gate 1's checks: the three that need no plan, same names and details as
    their halves of `validate()`. No `pack` param — the index at the pinned
    state is the ground truth, and `validate()`'s `pack` is already unused."""
    repo = _RepoPaths(repo_id)
    checks = [
        _path_validity(spec_output.spec, repo),
        _criteria_wellformed(spec_output.spec),
        _check("scope_consistency",
               _spec_scope_details(spec_output.spec, spec_output.open_questions)),
    ]
    return ValidationReport(passed=all(c.passed for c in checks), checks=checks)


def escalation_for_spec(spec: Spec) -> list[str]:
    """The escalation reasons a spec alone can raise."""
    settings = get_settings()
    if len(spec.touchable_files) > settings.max_touchable_files:
        return [
            f"{len(spec.touchable_files)} touchable files > "
            f"MAX_TOUCHABLE_FILES={settings.max_touchable_files}"
        ]
    return []


def _escalation_message(reasons: list[str]) -> str | None:
    return f"plan exceeds easy-medium thresholds ({'; '.join(reasons)})" if reasons else None


def escalation_for(output: SpecPlanOutput) -> str | None:
    """Not a failure — an honest 'this is bigger than the easy-medium lane'."""
    settings = get_settings()
    reasons = []
    if len(output.plan.tasks) > settings.max_plan_tasks:
        reasons.append(f"{len(output.plan.tasks)} tasks > MAX_PLAN_TASKS={settings.max_plan_tasks}")
    return _escalation_message(reasons + escalation_for_spec(output.spec))


# ============================================================== repair + loop


def render_repair_prompt(base_prompt: str, previous_json: str,
                         report: ValidationReport) -> str:
    """The generation prompt this call already used + the model's own previous
    JSON + every failed check's details verbatim. Augmented edges are NOT in
    `previous_json` — the caller passes the pre-augmentation JSON, so the model
    is never asked to own work the graph did for it."""
    failures = []
    for check in report.checks:
        if check.passed:
            continue
        failures.append(f"[{check.name}]")
        failures += [f"  - {d}" for d in check.details]
    return (
        f"{base_prompt}\n\n"
        f"YOUR PREVIOUS RESPONSE:\n```json\n{previous_json}\n```\n\n"
        f"REPAIR — deterministic validation of that response against the real repository "
        f"failed these checks:\n" + "\n".join(failures) + "\n\n"
        "Fix ONLY these issues; keep everything else identical. Emit the full corrected JSON."
    )


def spec_pipeline(request_text: str, repo_id, gateway: LLMGateway | None = None,
                  *, attachments: list[str] | None = None, max_repairs: int = 1,
                  pack: ContextPack | None = None, **kwargs) -> SpecResult:
    """Gate 1: Stage A -> generate spec -> validate_spec -> bounded repair.

    `pack` short-circuits Stage A when the caller already built one. Failing
    after `max_repairs` returns a SpecResult with passed=False — the caller
    decides what that means. Never loops unbounded.
    """
    from src.context import build_context  # lazy: keeps Stage A out of import cycles

    gateway = gateway or LLMGateway()
    if pack is None:
        pack = build_context(repo_id, request_text, attachments or [], gateway=gateway)

    output = generate_spec(request_text, pack, gateway, **kwargs)
    repair_attempts = 0
    for attempt in range(max_repairs + 1):
        report = validate_spec(output, repo_id)
        if report.passed or attempt == max_repairs:
            break
        output = gateway.complete_json(
            tier="strong",
            system=load_prompt("spec_only"),
            user=render_repair_prompt(render_user_prompt(request_text, pack),
                                      output.model_dump_json(indent=2), report),
            schema=SpecOutput,
            max_tokens=4096,
            purpose="spec_repair",
            **kwargs,
        )
        repair_attempts += 1

    return SpecResult(
        output=output, report=report,
        escalation=_escalation_message(escalation_for_spec(output.spec)),
        repair_attempts=repair_attempts,
    )


def plan_pipeline(request_text: str, repo_id, spec: SpecOutput,
                  gateway: LLMGateway | None = None, *, pack: ContextPack,
                  max_repairs: int = 1, **kwargs) -> PlanResult:
    """Gate 2: generate plan for an APPROVED spec -> assemble -> the full
    7-check validate -> bounded repair.

    `spec` and `pack` are both required: the spec was approved by a human at
    gate 1 and the pack was built at Stage A — this stage re-derives neither.
    Only the plan is regenerated on repair; the spec is copied through by
    `_assemble` every time.
    """
    gateway = gateway or LLMGateway()
    base_prompt = render_plan_prompt(request_text, pack, spec)

    plan = generate_plan(request_text, pack, spec, gateway, **kwargs)
    repair_attempts = 0
    for attempt in range(max_repairs + 1):
        pristine = plan.model_dump_json(indent=2)   # pre-augmentation, for the repair prompt
        output = _assemble(spec, plan)
        report = validate(output, pack, repo_id)
        if report.passed or attempt == max_repairs:
            break
        plan = gateway.complete_json(
            tier="strong",
            system=load_prompt("plan_only"),
            user=render_repair_prompt(base_prompt, pristine, report),
            schema=Plan,
            max_tokens=8192,
            purpose="plan_repair",
            **kwargs,
        )
        repair_attempts += 1

    return PlanResult(
        output=output, report=report,
        escalation=escalation_for(output), repair_attempts=repair_attempts,
        pack=pack,
    )
