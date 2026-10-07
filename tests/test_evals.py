"""Unit tests for the eval harness's pure logic: scorer functions (jaccard,
token_ratio) and replay_case/score_case against two synthetic cases (one
passing, one hard-gate-failing) — generate_fn/validate_fn/repo_id are
injected so nothing here touches a real LLM or DB.
"""

from __future__ import annotations

import json

from src.models import SpecPlanOutput, ValidationCheck, ValidationReport
from evals.run_evals import jaccard, load_case, load_cases, replay_case, score_case, token_ratio

# ------------------------------------------------------------------- fixtures


def _spec_plan(touchable_files=("app.py", "test_app.py"), n_criteria=1, n_tasks=2) -> dict:
    criteria = [
        {"id": f"AC{i+1}", "given": "a user opens the app", "when": "they click go",
         "then": "the app responds correctly", "verify_by": "unit"}
        for i in range(n_criteria)
    ]
    tasks = [
        {"id": f"t{i+1}", "title": f"Task {i+1}", "description": "Do the thing.",
         "files": [touchable_files[i % len(touchable_files)]], "done_criteria": "it works",
         "est_size": "S", "criterion_refs": [f"AC{(i % n_criteria) + 1}"]}
        for i in range(n_tasks)
    ]
    return {
        "spec": {
            "problem_statement": "fix things",
            "criteria": criteria,
            "touchable_files": list(touchable_files),
            "non_goals": [],
            "assumptions": [{"text": "the app is already running", "confidence": "high"}],
        },
        "open_questions": [],
        "plan": {"tasks": tasks, "edges": []},
    }


def _context_pack(paths=("app.py", "test_app.py")) -> dict:
    return {
        "repo_id": 1, "sha": "deadbeef", "request_type": "feature",
        "files": [{"path": p, "tier": 1, "label": "critical", "reason": "seed", "content": ""} for p in paths],
        "seed_symbols": [], "recent_commits": [], "token_count": 10, "stats": {},
    }


def _case(name: str = "case1") -> dict:
    return {
        "name": name, "run_id": "r1", "repo_root_path": "/tmp/repo", "indexed_sha": "deadbeef",
        "request": {"type": "feature", "text": "add a widget", "attachments": []},
        "context_pack": _context_pack(),
        "spec_plan": _spec_plan(),
        "validation": {"passed": True, "checks": [], "escalation": None, "repair_attempts": 0},
    }


_PASS = ValidationReport(passed=True, checks=[])
_FAIL = ValidationReport(passed=False, checks=[ValidationCheck(name="path_validity", passed=False,
                                                                 details=["bad path"])])


# ------------------------------------------------------------------ scorers


def test_jaccard_full_overlap():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_no_overlap():
    assert jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_both_empty_is_one():
    assert jaccard(set(), set()) == 1.0


def test_jaccard_partial_overlap():
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3


def test_token_ratio_identical_text_is_one():
    assert token_ratio("hello world", "world hello") == 1.0  # order-insensitive


def test_token_ratio_disjoint_text_is_low():
    # difflib.SequenceMatcher works on the joined strings, so completely
    # disjoint words still share some characters — assert "low", not zero.
    assert token_ratio("alpha beta", "gamma delta") < 0.5


def test_token_ratio_case_insensitive():
    assert token_ratio("Hello World", "hello world") == 1.0


# --------------------------------------------------------------------- score_case


def test_score_case_identical_output_passes():
    case = _case()
    fresh = SpecPlanOutput.model_validate(_spec_plan())
    result = score_case(case, fresh, _PASS)
    assert result["passed"] is True
    assert result["hard_gate_failures"] == []
    assert result["soft_metrics"]["touchable_files_jaccard"] == 1.0
    assert result["soft_metrics"]["criteria_text_ratio"] == 1.0


def test_score_case_fails_on_validation_report():
    case = _case()
    fresh = SpecPlanOutput.model_validate(_spec_plan())
    result = score_case(case, fresh, _FAIL)
    assert result["passed"] is False
    assert any("validation report failed" in f for f in result["hard_gate_failures"])


def test_score_case_fails_on_stray_touchable_file():
    case = _case()
    fresh = SpecPlanOutput.model_validate(_spec_plan(touchable_files=("app.py", "test_app.py", "other.py")))
    result = score_case(case, fresh, _PASS)
    assert result["passed"] is False
    assert any("other.py" in f for f in result["hard_gate_failures"])


def test_score_case_fails_on_criteria_count_delta():
    case = _case()  # golden has 1 criterion
    fresh = SpecPlanOutput.model_validate(_spec_plan(n_criteria=4, n_tasks=4))  # +3
    result = score_case(case, fresh, _PASS)
    assert result["passed"] is False
    assert any("criteria count delta" in f for f in result["hard_gate_failures"])


def test_score_case_fails_on_task_count_delta():
    case = _case()  # golden has 2 tasks
    fresh = SpecPlanOutput.model_validate(_spec_plan(n_tasks=6))  # +4, over the ±2 gate
    result = score_case(case, fresh, _PASS)
    assert result["passed"] is False
    assert any("task count delta" in f for f in result["hard_gate_failures"])


def test_score_case_allows_touchable_file_thats_only_in_the_pack():
    # in golden's context pack but not its touchable_files -> still allowed
    case = _case()
    case["context_pack"] = _context_pack(paths=("app.py", "test_app.py", "helpers.py"))
    fresh = SpecPlanOutput.model_validate(_spec_plan(touchable_files=("app.py", "helpers.py")))
    result = score_case(case, fresh, _PASS)
    assert result["passed"] is True


# --------------------------------------------------------------------- replay_case


def test_replay_case_passing():
    case = _case()

    def fake_plan(text, pack, repo_id):
        assert repo_id == 42
        return SpecPlanOutput.model_validate(_spec_plan()), _PASS

    result = replay_case(case, plan_fn=fake_plan, repo_id=42)
    assert result["passed"] is True
    assert result["name"] == "case1"


def test_replay_case_hard_gate_failing():
    case = _case()

    def fake_plan(text, pack, repo_id):
        fresh = SpecPlanOutput.model_validate(_spec_plan(touchable_files=("app.py", "test_app.py", "sneaky.py")))
        return fresh, _PASS

    result = replay_case(case, plan_fn=fake_plan, repo_id=42)
    assert result["passed"] is False
    assert result["hard_gate_failures"]


# ---------------------------------------------------------------------- case IO


def test_load_case_and_load_cases(tmp_path):
    case = _case("from_disk")
    p = tmp_path / "from_disk.json"
    p.write_text(json.dumps(case))

    assert load_case(p) == case
    assert load_cases(tmp_path) == [case]
