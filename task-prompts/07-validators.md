# Task 07: Validators + Repair Loop (the accuracy core)

## Context

Done: through task 06 (generate_spec_plan producing SpecPlanOutput). This task adds the deterministic checks that catch LLM errors, the symbol-graph edge augmentation, the bounded repair loop, and the escalation flag. LLM proposes, this code disposes.

## Files to create/modify

```
src/planning.py          # ADD: validate(), augment_edges(), repair loop, plan_pipeline()
src/models.py            # ADD: ValidationCheck, ValidationReport, PlanResult
tests/test_validators.py
```

## Models

```python
class ValidationCheck(BaseModel):
    name: str; passed: bool; details: list[str]   # each detail actionable, e.g. "task t3 file 'src/foo.ts' not in touchable_files"

class ValidationReport(BaseModel):
    passed: bool; checks: list[ValidationCheck]

class PlanResult(BaseModel):
    output: SpecPlanOutput
    report: ValidationReport
    escalation: str | None       # set when size thresholds tripped
    repair_attempts: int
```

## The 7 validators — pure code, no LLM

`validate(output: SpecPlanOutput, pack: ContextPack, repo_id) -> ValidationReport`

1. **path_validity** — every touchable_file and every task file: exists in the index at the repo's indexed state (via index_query), OR is new with (a) parent directory existing in the index and (b) extension in SUPPORTED_EXTENSIONS or a test-file convention. Non-goals must reference existing paths/modules.
2. **criteria_wellformed** — every criterion has non-empty given/when/then and verify_by (pydantic covers types; check non-trivial content, e.g. ≥3 words each).
3. **scope_consistency** — no path both touchable and inside a non-goal; every task file ∈ touchable_files ∪ {valid new files declared in touchable_files}; open_questions ≤ 3 with non-empty defaults.
4. **edge_augmentation** — for each ordered task pair (A,B): if any symbol defined in B's files has a `resolved` edge (imports/calls) FROM symbols in A's files' — wait, direction: if B's files depend on (import/call) symbols defined in A's files and no A→B edge exists, ADD TaskEdge(A→B, kind="code"). Only `resolved` confidence edges; never add from heuristic. This MUTATES the plan (augmentation, not just validation) — record additions in check details.
5. **dag_acyclic** — topological sort over tasks+edges (including augmented); cycle → fail with the cycle listed.
6. **traceability** — every criterion referenced by ≥1 task; every task references ≥1 existing criterion id.
7. **test_coverage** — every criterion with verify_by unit|integration is referenced by at least one task that touches a test file (test-convention paths, both ecosystems) or whose title/description contains "test".

Escalation flag (not a failure): tasks > MAX_PLAN_TASKS OR touchable_files > MAX_TOUCHABLE_FILES → `escalation = "plan exceeds easy-medium thresholds (...)"`. Both thresholds live in src/config.py, env-overridable (`MAX_PLAN_TASKS` default 8, `MAX_TOUCHABLE_FILES` default 12) — appropriate size varies per repo/team, so this must be tunable without code change.

## Repair loop + pipeline

```python
def plan_pipeline(request_text, repo_id, gateway, max_repairs: int = 1) -> PlanResult:
    pack = build_context(...)
    out = generate_spec_plan(...)
    for attempt in range(max_repairs + 1):
        out = augment_and_validate...(mutating augmentation first, then checks)
        if report.passed: break
        if attempt < max_repairs:
            out = regenerate with repair prompt: original prompt + the model's
                  previous JSON + a REPAIR section listing every failed check's
                  details verbatim + "Fix ONLY these issues; keep everything else identical."
    return PlanResult(...)
```
- Repair is a fresh `complete_json` strong call (purpose="spec_plan_repair").
- After max_repairs still failing → return PlanResult with passed=False; caller decides (task 08 marks run failed). Never loop unbounded, never silently drop failing parts.
- Augmented edges from a previous attempt are not fed back as if the model produced them — augmentation re-runs each attempt on the model's own output.

## Tests (tests/test_validators.py) — no LLM for validators; fake gateway for the loop

Construct SpecPlanOutput fixtures programmatically against the indexed sample_repo:
1. path_validity: nonexistent task file fails with actionable detail; valid new test file under existing dir passes; new file in nonexistent dir fails.
2. scope_consistency: file in both touchable and non_goals fails; task file outside touchable fails.
3. edge_augmentation: two tasks where B's file imports A's file (use the list.tsx → dashboard.tsx fixture edge from task 03) and no edge given → code edge added, recorded in details; heuristic-only dependency does NOT add an edge.
4. dag_acyclic: constructed cycle (t1→t2→t1) fails naming the cycle; augmentation-induced cycle also caught.
5. traceability: orphan task (no criterion_refs) fails; uncovered criterion fails.
6. test_coverage: criterion verify_by=unit with no test task fails; passes when a task touches tests/... path (py) or __tests__/... (ts).
7. Escalation: 9 tasks → escalation set with default thresholds, report can still pass; with MAX_PLAN_TASKS=20 the same plan does not escalate (env override works).
8. Repair loop: fake gateway returns invalid-then-valid output → repair_attempts==1, final passed; repair prompt contained the failed check details. Always-invalid fake → passed=False after max_repairs, no exception.

## Real-repo smoke (definition of done, real LLM)

Run `plan_pipeline` end-to-end on 2 requests per repo:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Report per run: validation report (all checks), augmented-edge count, repair attempts, escalation flag, and the final plan. At least one run should be a request crafted to trip a validator on first generation (e.g. a request tempting the model to touch a file you exclude in phrasing) to demonstrate the repair loop firing in the wild — report before/after.

## Out of scope

Runs/state machine/persistence of results (task 08), partial regeneration, approval flow, UI.
