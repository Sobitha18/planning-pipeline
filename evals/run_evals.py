"""Replay every stored golden case against the CURRENT prompts/model and
score the fresh output vs. the golden reference.

    python -m evals.run_evals

Replays Stage B only (context-stage replay is out of scope here — the
stored context_pack IS the input, unchanged; that's the case the design doc
flags as the one that matters most since prompts change more than
retrieval), through the SAME two-gate generate -> validate -> bounded-repair
path production runs: `spec_pipeline` then `plan_pipeline` over its output,
both against the stored pack (no Stage A re-run). The human approval between
the two gates is the only thing skipped — the spec_pipeline's spec goes
straight into plan_pipeline, exactly as an approve-without-edits would.
Golden cases were themselves captured after those repair loops ran, so
replaying without them would be a systematically unfair comparison. The
scored artifact is still the assembled `SpecPlanOutput`, whose shape is
unchanged. Requires a live Anthropic API key (Stage B is real LLM calls)
and the repo the case names to still be indexed under the same root_path
(validate() checks paths against the live index) — same DB conventions as
the rest of the test suite.

Hard gates (any failure -> case fails, exit code 1):
  - validation report must pass
  - touchable_files subset of (golden touchable_files ∪ golden pack paths)
  - criteria count within ±1 of golden
  - task count within ±2 of golden

Soft metrics (reported, never fail the case): jaccard of touchable_files,
jaccard of task-file sets, criteria text token-set ratio, open-question
count delta.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

from src import index_query as iq
from src.models import ContextPack, SpecPlanOutput, ValidationReport
from src.planning import plan_pipeline, spec_pipeline

CASES_DIR = Path(__file__).parent / "cases"


# --------------------------------------------------------------------- scorers


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def token_ratio(a: str, b: str) -> float:
    """Embedding-free text similarity: sort+dedupe each side's word tokens,
    then a difflib ratio over the resulting strings (order-insensitive,
    unlike a raw SequenceMatcher over the prose itself)."""
    ta = " ".join(sorted(set(re.findall(r"\w+", a.lower()))))
    tb = " ".join(sorted(set(re.findall(r"\w+", b.lower()))))
    return difflib.SequenceMatcher(None, ta, tb).ratio()


# ---------------------------------------------------------------------- cases


def load_case(path: Path) -> dict:
    return json.loads(path.read_text())


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    return [load_case(p) for p in sorted(cases_dir.glob("*.json"))]


# ----------------------------------------------------------------- scoring core


def score_case(case: dict, fresh_output: SpecPlanOutput, fresh_report: ValidationReport) -> dict:
    golden = SpecPlanOutput.model_validate(case["spec_plan"])
    golden_touchable = set(golden.spec.touchable_files)
    golden_pack_paths = {f["path"] for f in case["context_pack"].get("files", [])}
    allowed = golden_touchable | golden_pack_paths

    fresh_touchable = set(fresh_output.spec.touchable_files)
    fresh_task_files = {f for t in fresh_output.plan.tasks for f in t.files}
    golden_task_files = {f for t in golden.plan.tasks for f in t.files}

    failures: list[str] = []
    if not fresh_report.passed:
        failed_checks = ", ".join(c.name for c in fresh_report.checks if not c.passed)
        failures.append(f"validation report failed: {failed_checks}")

    stray = fresh_touchable - allowed
    if stray:
        failures.append(f"touchable_files outside golden touchable ∪ pack paths: {sorted(stray)}")

    criteria_delta = len(fresh_output.spec.criteria) - len(golden.spec.criteria)
    if abs(criteria_delta) > 1:
        failures.append(f"criteria count delta {criteria_delta:+d} exceeds ±1 (golden had "
                         f"{len(golden.spec.criteria)}, fresh has {len(fresh_output.spec.criteria)})")

    task_delta = len(fresh_output.plan.tasks) - len(golden.plan.tasks)
    if abs(task_delta) > 2:
        failures.append(f"task count delta {task_delta:+d} exceeds ±2 (golden had "
                         f"{len(golden.plan.tasks)}, fresh has {len(fresh_output.plan.tasks)})")

    def criteria_text(spec) -> str:
        return " ".join(f"{c.given} {c.when} {c.then}" for c in spec.criteria)

    soft = {
        "touchable_files_jaccard": round(jaccard(fresh_touchable, golden_touchable), 3),
        "task_files_jaccard": round(jaccard(fresh_task_files, golden_task_files), 3),
        "criteria_text_ratio": round(token_ratio(criteria_text(fresh_output.spec), criteria_text(golden.spec)), 3),
        "open_question_count_delta": len(fresh_output.open_questions) - len(golden.open_questions),
    }

    return {
        "name": case["name"],
        "passed": not failures,
        "hard_gate_failures": failures,
        "soft_metrics": soft,
    }


def _default_plan_fn(request_text: str, pack: ContextPack, repo_id):
    """Production Stage B path, both gates: spec_pipeline's approved spec feeds
    plan_pipeline, and both take the stored `pack=` so Stage A (retrieval) never
    re-runs. Returns the assembled SpecPlanOutput and the full 7-check report,
    which is what score_case grades."""
    spec_result = spec_pipeline(request_text, repo_id, pack=pack, max_repairs=1)
    plan_result = plan_pipeline(request_text, repo_id, spec_result.output,
                                pack=pack, max_repairs=1)
    return plan_result.output, plan_result.report


def replay_case(case: dict, *, plan_fn=None, repo_id=None) -> dict:
    """Stage B replay: same stored context pack + request text -> fresh
    generate/validate/repair -> score vs. golden. `plan_fn`/`repo_id` are
    injectable so tests can replay without a live LLM or DB; `plan_fn` takes
    (request_text, pack, repo_id) and returns (SpecPlanOutput, ValidationReport)."""
    pack = ContextPack.model_validate(case["context_pack"])
    plan_fn = plan_fn or _default_plan_fn
    if repo_id is None:
        repo_id = iq.resolve_repo_id(case["repo_root_path"])
        if repo_id is None:
            raise RuntimeError(
                f"case {case['name']!r}: repo {case['repo_root_path']!r} is not indexed in the "
                f"current DB — index it (POST /v1/repos) before running evals"
            )

    fresh_output, fresh_report = plan_fn(case["request"]["text"], pack, repo_id)
    return score_case(case, fresh_output, fresh_report)


# -------------------------------------------------------------------- reporting


def _print_report(results: list[dict]) -> None:
    width = max((len(r["name"]) for r in results), default=4)
    print(f"{'CASE':<{width}}  RESULT")
    print("-" * (width + 10))
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['name']:<{width}}  {status}")
        if not r["passed"]:
            for f in r["hard_gate_failures"]:
                print(f"    - {f}")
        for k, v in r["soft_metrics"].items():
            print(f"    · {k}: {v}")
    n_pass = sum(r["passed"] for r in results)
    print("-" * (width + 10))
    print(f"{n_pass}/{len(results)} passed")


def main(cases_dir: Path = CASES_DIR) -> int:
    cases = load_cases(cases_dir)
    if not cases:
        print(f"no cases found in {cases_dir}", file=sys.stderr)
        return 1
    results = [replay_case(case) for case in cases]
    _print_report(results)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
