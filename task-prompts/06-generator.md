# Task 06: Stage B — Spec + Plan Generator

## Context

Done: extractor, indexer, edges, LLM gateway, Stage A (ContextPack). This task: ONE strong-LLM call turning (request + ContextPack) into spec + open questions + task DAG. Validation of the output is task 07 — here we only generate, with schema-valid structure guaranteed by the gateway.

## Files to create

```
src/models.py           # ADD: Spec, Criterion, Assumption, OpenQuestion, Task, TaskEdge, Plan, SpecPlanOutput
src/prompts/spec_plan.md
src/planning.py         # generate_spec_plan(request, pack) -> SpecPlanOutput  (validators added here in task 07)
tests/test_planning_gen.py
```

## Data models

```python
class Criterion(BaseModel):
    id: str                      # "AC1"
    given: str; when: str; then: str
    verify_by: Literal["unit","integration","manual"]

class Assumption(BaseModel):
    text: str; confidence: Literal["high","low"]

class OpenQuestion(BaseModel):
    q: str; default: str         # default = binding assumption if skipped

class Spec(BaseModel):
    problem_statement: str
    criteria: list[Criterion]            # 1-4
    touchable_files: list[str]           # repo-relative paths
    non_goals: list[str]
    assumptions: list[Assumption]

class Task(BaseModel):
    id: str                      # "t1"
    title: str
    description: str             # 2-4 sentences: what to do and how to know it's done
    files: list[str]
    done_criteria: str
    est_size: Literal["S","M","L"]
    criterion_refs: list[str]

class TaskEdge(BaseModel):
    from_task: str; to_task: str
    kind: Literal["data","code","contract"]

class Plan(BaseModel):
    tasks: list[Task]            # expect 1-6; >8 triggers escalation flag (task 07)
    edges: list[TaskEdge]

class SpecPlanOutput(BaseModel):
    spec: Spec
    open_questions: list[OpenQuestion]   # max 3
    plan: Plan
```

## The prompt (src/prompts/spec_plan.md) — the core deliverable

Must instruct the model to:
1. Read the request and Context Pack (files with tiers/labels/content).
2. Write the spec: problem statement; 1-4 criteria in strict Given/When/Then each naming verify_by; unverifiable behavior is NOT a criterion.
3. **touchable_files chosen ONLY from Context Pack file paths.** New files allowed only when clearly needed (a new component/module/test/migration), placed in an existing directory visible in the pack, following the naming style of sibling files. State in assumptions when a new file is proposed.
4. non_goals: 1-3 adjacent modules/files visible in the pack that will NOT be touched, phrased "Will not modify X because Y" — make scope creep explicit.
5. assumptions: everything assumed; mark low confidence only where a wrong guess would change the plan. open_questions ONLY for low-confidence blocking assumptions, max 3, each with a sensible default.
6. plan tasks: ordered along layers actually present in the touched files (data/schema → domain/server logic → API/route → UI component → tests); skip absent layers; every criterion covered by ≥1 task AND reachable by a test-writing task; task descriptions concrete (name the files and the change, not "update the code"); est_size S≈<50 changed LOC, M≈50-200, L≈>200.
7. edges: data (B consumes A's artifact), code (B's files depend on A's files), contract (interface/schema task precedes consumers). No cycles.
8. Language/stack-neutral phrasing: works for a Next.js repo (components, server actions, API routes, Prisma) and a Python repo (modules, services, endpoints) — infer the stack from the pack, don't assume.

Include 1 compact worked example in the prompt (a small bug on a fictional 3-file pack → full SpecPlanOutput JSON) — few-shot anchors the shape.

## Generator function

```python
def generate_spec_plan(request_text: str, pack: ContextPack,
                       gateway: LLMGateway) -> SpecPlanOutput
```
- Renders the prompt template with request + pack (pack serialization: per file, path + tier + label + content).
- Calls `gateway.complete_json(tier="strong", schema=SpecPlanOutput, max_tokens=8192, purpose="spec_plan")`.
- Pure generation: no validation logic here beyond pydantic schema (that's task 07). Returns the parsed object.

## Tests (tests/test_planning_gen.py) — mocked gateway, no network

1. Prompt rendering: given a small fake pack, the rendered user prompt contains every pack file path and the request text; tier-1 content included verbatim; tier-3 files as path+reason only.
2. Happy path: fake gateway returns a valid SpecPlanOutput JSON → parsed object round-trips.
3. Schema strictness: criterion missing `verify_by`, question count 4, invalid est_size each fail pydantic validation (i.e., gateway retry path would trigger — assert LLMValidationError surfaces when fake always returns the bad payload).
4. Determinism of serialization: same pack → identical prompt string (stable ordering).

## Real-repo smoke (definition of done, real LLM)

Reuse the 6 requests from task 05's smoke (3 per repo) on:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
For each: build_context → generate_spec_plan → pretty-print spec + plan. Manual review checklist to report on:
- touchable_files all exist in the pack (or are sensibly-new)
- criteria are genuinely testable
- tasks reference real files, ordering sensible, test task present
- non_goals name real adjacent code
Note failures verbatim — they feed prompt iteration; do not silently tweak the prompt to pass, report first.

## Out of scope

All 7 validators + repair loop (task 07), runs/state machine, partial regeneration, escalation handling (07), UI.
