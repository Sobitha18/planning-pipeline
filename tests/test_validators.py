"""Task 07: the 7 validators, edge augmentation, escalation and the repair
loop — all against the real indexed sample_repo. No network: the validators
never call an LLM, and the repair-loop tests use a fake gateway.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src import index_query as iq
from src.indexer.edges import build_edges
from src.indexer.full_index import index_repo
from src.models import (
    ContextPack,
    PackedFile,
    SpecOutput,
    SpecPlanOutput,
)
from src.planning import augment_edges, plan_pipeline, spec_pipeline, validate, validate_spec

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="module")
def repo_id(db_url, tmp_path_factory):
    """Index + edge-build the sample repo once for the whole module (the
    validators only read it)."""
    dest = tmp_path_factory.mktemp("vrepo") / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    assert index_repo(str(dest)).errors == []
    rid = iq.resolve_repo_id(str(dest))
    build_edges(rid)
    return rid


@pytest.fixture
def pack(repo_id):
    """Validators do not read the pack (the index is the ground truth), but
    every call takes one."""
    return ContextPack(
        repo_id=repo_id, sha="deadbeef",
        files=[PackedFile(path="src/components/dashboard.tsx", tier=1, label="critical",
                          reason="the Button lives here", content="…")],
    )


# ------------------------------------------------------------------ builders


def spec_plan(**overrides) -> SpecPlanOutput:
    """A valid baseline against sample_repo; override any leaf via kwargs."""
    payload = {
        "spec": {
            "problem_statement": "Button needs a disabled state in the dashboard.",
            "criteria": [{
                "id": "AC1", "given": "a Button with disabled set",
                "when": "the user clicks it", "then": "no onClick handler runs",
                "verify_by": "unit",
            }],
            "touchable_files": [
                "src/components/dashboard.tsx",
                "src/components/__tests__/dashboard.test.tsx",
            ],
            "non_goals": ["Will not modify src/lib/prisma.ts because no data access changes"],
            "assumptions": [{"text": "Button is the only affected component", "confidence": "high"}],
        },
        "open_questions": [],
        "plan": {
            "tasks": [
                {"id": "t1", "title": "Add disabled prop", "description": "Add a disabled prop to Button.",
                 "files": ["src/components/dashboard.tsx"], "done_criteria": "clicks are ignored",
                 "est_size": "S", "criterion_refs": ["AC1"]},
                {"id": "t2", "title": "Test disabled Button", "description": "Cover the disabled path.",
                 "files": ["src/components/__tests__/dashboard.test.tsx"],
                 "done_criteria": "tests pass", "est_size": "S", "criterion_refs": ["AC1"]},
            ],
            "edges": [{"from_task": "t1", "to_task": "t2", "kind": "code"}],
        },
    }
    for dotted, value in overrides.items():
        node = payload
        *path, leaf = dotted.split("__")
        for key in path:
            node = node[int(key) if key.isdigit() else key]
        node[int(leaf) if leaf.isdigit() else leaf] = value
    return SpecPlanOutput.model_validate(payload)


def spec_only(**overrides) -> SpecOutput:
    """The spec half of the same baseline — what gate 1 approves."""
    out = spec_plan(**overrides)
    return SpecOutput(spec=out.spec, open_questions=out.open_questions)


def check(report, name):
    return next(c for c in report.checks if c.name == name)


def failed(report) -> set[str]:
    return {c.name for c in report.checks if not c.passed}


# ------------------------------------------------------------ 0. happy path


def test_valid_plan_passes_every_check(repo_id, pack):
    report = validate(spec_plan(), pack, repo_id)
    assert report.passed, [c for c in report.checks if not c.passed]
    assert [c.name for c in report.checks] == [
        "path_validity", "criteria_wellformed", "scope_consistency",
        "edge_augmentation", "dag_acyclic", "traceability", "test_coverage",
    ]


# --------------------------------------------------------- 1. path_validity


def test_nonexistent_file_fails_with_actionable_detail(repo_id, pack):
    out = spec_plan(
        spec__touchable_files=["src/components/ghost.tsx",
                               "src/components/__tests__/dashboard.test.tsx"],
        plan__tasks__0__files=["src/components/ghost.tsx"],
    )
    report = validate(out, pack, repo_id)
    assert "path_validity" not in failed(report)  # ghost.tsx's dir exists -> "new file", legal
    out = spec_plan(
        spec__touchable_files=["src/nope/ghost.tsx",
                               "src/components/__tests__/dashboard.test.tsx"],
        plan__tasks__0__files=["src/nope/ghost.tsx"],
    )
    report = validate(out, pack, repo_id)
    detail = check(report, "path_validity").details[0]
    assert "src/nope/ghost.tsx" in detail and "src/nope" in detail
    assert not report.passed


def test_new_test_file_under_existing_dir_passes(repo_id, pack):
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx",
                               "src/components/__tests__/button.test.tsx"],
        plan__tasks__1__files=["src/components/__tests__/button.test.tsx"],
    )
    report = validate(out, pack, repo_id)
    assert check(report, "path_validity").passed, check(report, "path_validity").details


def test_unsupported_extension_new_file_fails(repo_id, pack):
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", "src/components/notes.docx",
                               "src/components/__tests__/dashboard.test.tsx"],
    )
    report = validate(out, pack, repo_id)
    assert "unsupported extension" in " ".join(check(report, "path_validity").details)


def test_non_goal_naming_a_route_group_path_passes(monkeypatch):
    """Next.js route groups / dynamic segments put parens and brackets inside
    real paths — the non-goal tokenizer must not split them into prose."""
    from src.planning import _RepoPaths

    monkeypatch.setattr(iq, "list_paths",
                        lambda _rid: ["src/app/(protected)/do/[id]/page.tsx"])
    repo = _RepoPaths(0)
    assert repo.mentions_something_real(
        "Will not modify src/app/(protected)/do/[id]/page.tsx because it only renders."
    )
    assert repo.mentions_something_real("Will not touch src/app/(protected) because scope.")
    assert not repo.mentions_something_real("Will not modify the billing subsystem.")


def test_non_goal_naming_nothing_real_fails(repo_id, pack):
    out = spec_plan(spec__non_goals=["Will not modify the billing subsystem because scope"])
    report = validate(out, pack, repo_id)
    assert not check(report, "path_validity").passed
    assert "billing subsystem" in check(report, "path_validity").details[0]


# ----------------------------------------------------- 2. criteria_wellformed


def test_thin_criterion_clause_fails(repo_id, pack):
    out = spec_plan(spec__criteria__0__then="works")
    report = validate(out, pack, repo_id)
    assert not check(report, "criteria_wellformed").passed
    assert "AC1" in check(report, "criteria_wellformed").details[0]


# ------------------------------------------------------ 3. scope_consistency


def test_file_both_touchable_and_non_goal_fails(repo_id, pack):
    out = spec_plan(
        spec__non_goals=["Will not modify src/components/dashboard.tsx because it is stable"],
    )
    report = validate(out, pack, repo_id)
    assert not check(report, "scope_consistency").passed
    assert "src/components/dashboard.tsx" in check(report, "scope_consistency").details[0]


def test_task_file_outside_touchable_fails(repo_id, pack):
    out = spec_plan(plan__tasks__0__files=["src/lib/prisma.ts"])
    report = validate(out, pack, repo_id)
    details = check(report, "scope_consistency").details
    assert any("t1" in d and "src/lib/prisma.ts" in d for d in details)


def test_open_question_without_default_fails(repo_id, pack):
    out = spec_plan(open_questions=[{"q": "Ship behind a flag?", "default": "  "}])
    report = validate(out, pack, repo_id)
    assert not check(report, "scope_consistency").passed


def test_open_question_with_one_option_fails(repo_id, pack):
    out = spec_plan(open_questions=[
        {"q": "Ship behind a flag?", "options": ["Yes"], "default": "Yes"},
    ])
    report = validate(out, pack, repo_id)
    details = check(report, "scope_consistency").details
    assert any("Ship behind a flag?" in d and "1 options" in d for d in details)


def test_open_question_default_not_in_options_fails(repo_id, pack):
    out = spec_plan(open_questions=[
        {"q": "Ship behind a flag?", "options": ["Yes", "No"], "default": "Maybe"},
    ])
    report = validate(out, pack, repo_id)
    details = check(report, "scope_consistency").details
    assert any("not one of its options" in d for d in details)


def test_five_open_questions_with_options_passes(repo_id, pack):
    """Proves the ceiling actually moved 3 -> 5: pydantic's own max_length on
    SpecPlanOutput.open_questions would reject a 6th before this check ever
    ran, so 5-passes is the reachable half of that change to assert on."""
    questions = [
        {"q": f"q{i}?", "options": ["a", "b"], "default": "a"} for i in range(5)
    ]
    out = spec_plan(open_questions=questions)
    report = validate(out, pack, repo_id)
    assert check(report, "scope_consistency").passed


# ------------------------------------------------------ 4. edge_augmentation


def test_resolved_dependency_adds_code_edge(repo_id):
    """list.tsx imports+calls Button from dashboard.tsx (resolved edge, task 03)."""
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", "src/components/list.tsx"],
        plan__tasks__0__files=["src/components/dashboard.tsx"],
        plan__tasks__1__files=["src/components/list.tsx"],
        plan__edges=[],
    )
    result = augment_edges(out, repo_id)
    assert result.passed
    assert [(e.from_task, e.to_task, e.kind) for e in out.plan.edges] == [("t1", "t2", "code")]
    assert "t1 -> t2" in result.details[0]
    assert "src/components/dashboard.tsx" in result.details[0]


def test_existing_edge_is_not_duplicated(repo_id):
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", "src/components/list.tsx"],
        plan__tasks__0__files=["src/components/dashboard.tsx"],
        plan__tasks__1__files=["src/components/list.tsx"],
    )
    assert augment_edges(out, repo_id).details == []
    assert len(out.plan.edges) == 1


def test_file_level_import_alone_adds_edge(repo_id, monkeypatch):
    """No symbol edge (the import is used at module top level, outside every
    indexed symbol) — the resolved file import is still proof of dependency."""
    monkeypatch.setattr(iq, "neighbors", lambda *a, **k: [])
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", "src/components/list.tsx"],
        plan__tasks__0__files=["src/components/dashboard.tsx"],
        plan__tasks__1__files=["src/components/list.tsx"],
        plan__edges=[],
    )
    assert augment_edges(out, repo_id).details
    assert [(e.from_task, e.to_task) for e in out.plan.edges] == [("t1", "t2")]


def test_heuristic_dependency_adds_no_edge(repo_id):
    """service.py's ambiguous_caller -> helpers.parse_claims is heuristic only."""
    out = spec_plan(
        spec__touchable_files=["src/auth/helpers.py", "src/auth/service.py"],
        plan__tasks__0__files=["src/auth/helpers.py"],
        plan__tasks__1__files=["src/auth/service.py"],
        plan__edges=[],
    )
    assert augment_edges(out, repo_id).details == []
    assert out.plan.edges == []


# ------------------------------------------------------------ 5. dag_acyclic


def test_cycle_fails_and_names_the_cycle(repo_id, pack):
    out = spec_plan(plan__edges=[{"from_task": "t1", "to_task": "t2", "kind": "code"},
                                 {"from_task": "t2", "to_task": "t1", "kind": "code"}])
    report = validate(out, pack, repo_id)
    detail = check(report, "dag_acyclic").details[0]
    assert "t1 -> t2 -> t1" in detail or "t2 -> t1 -> t2" in detail


def test_augmentation_induced_cycle_is_caught(repo_id, pack):
    """The model declares list.tsx -> dashboard.tsx; the graph proves the
    reverse dependency, so augmentation creates a cycle and the DAG check
    must catch it rather than the plan shipping with it."""
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", "src/components/list.tsx"],
        plan__tasks__0__files=["src/components/dashboard.tsx"],
        plan__tasks__1__files=["src/components/list.tsx"],
        plan__edges=[{"from_task": "t2", "to_task": "t1", "kind": "contract"}],
    )
    report = validate(out, pack, repo_id)
    assert check(report, "edge_augmentation").details  # it did add t1 -> t2
    assert not check(report, "dag_acyclic").passed


def test_edge_to_unknown_task_fails(repo_id, pack):
    out = spec_plan(plan__edges=[{"from_task": "t1", "to_task": "t9", "kind": "code"}])
    report = validate(out, pack, repo_id)
    assert "t9" in check(report, "dag_acyclic").details[0]


# ----------------------------------------------------------- 6. traceability


def test_orphan_task_fails(repo_id, pack):
    out = spec_plan(plan__tasks__1__criterion_refs=[])
    report = validate(out, pack, repo_id)
    assert not check(report, "traceability").passed
    assert "t2" in check(report, "traceability").details[0]


def test_uncovered_criterion_fails(repo_id, pack):
    out = spec_plan(spec__criteria=[
        {"id": "AC1", "given": "a Button with disabled set", "when": "the user clicks it",
         "then": "no onClick handler runs", "verify_by": "unit"},
        {"id": "AC2", "given": "a Button without the prop", "when": "the user clicks it",
         "then": "the handler runs as before", "verify_by": "unit"},
    ])
    report = validate(out, pack, repo_id)
    assert "AC2" in " ".join(check(report, "traceability").details)


def test_reference_to_nonexistent_criterion_fails(repo_id, pack):
    out = spec_plan(plan__tasks__0__criterion_refs=["AC7"])
    report = validate(out, pack, repo_id)
    assert "AC7" in check(report, "traceability").details[0]


# ---------------------------------------------------------- 7. test_coverage


def test_criterion_without_test_task_fails(repo_id, pack):
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx"],
        plan__tasks=[{"id": "t1", "title": "Add disabled prop", "description": "Add the prop.",
                      "files": ["src/components/dashboard.tsx"], "done_criteria": "ignored clicks",
                      "est_size": "S", "criterion_refs": ["AC1"]}],
        plan__edges=[],
    )
    report = validate(out, pack, repo_id)
    assert not check(report, "test_coverage").passed
    assert "AC1" in check(report, "test_coverage").details[0]


def test_manual_criterion_needs_no_test_task(repo_id, pack):
    out = spec_plan(
        spec__criteria=[{"id": "AC1", "given": "the dashboard is open", "when": "the page renders",
                         "then": "the button looks visually disabled", "verify_by": "manual"}],
        spec__touchable_files=["src/components/dashboard.tsx"],
        plan__tasks=[{"id": "t1", "title": "Style it", "description": "Grey out the button.",
                      "files": ["src/components/dashboard.tsx"], "done_criteria": "looks grey",
                      "est_size": "S", "criterion_refs": ["AC1"]}],
        plan__edges=[],
    )
    assert check(validate(out, pack, repo_id), "test_coverage").passed


@pytest.mark.parametrize("test_file", [
    "tests/test_jwt.py", "src/components/__tests__/dashboard.test.tsx",
])
def test_both_ecosystem_test_conventions_count(repo_id, pack, test_file):
    out = spec_plan(
        spec__touchable_files=["src/components/dashboard.tsx", test_file],
        plan__tasks__1__title="Cover the new path",           # no "test" in the title
        plan__tasks__1__description="Add coverage for the new behaviour.",
        plan__tasks__1__files=[test_file],
    )
    assert check(validate(out, pack, repo_id), "test_coverage").passed


# -------------------------------------------------------------- escalation


def _nine_tasks() -> dict:
    return {
        "plan__tasks": [
            {"id": f"t{i}", "title": f"Step {i}", "description": "Do the thing with tests.",
             "files": ["src/components/dashboard.tsx"], "done_criteria": "done",
             "est_size": "S", "criterion_refs": ["AC1"]} for i in range(1, 10)
        ],
        "plan__edges": [],
        "spec__touchable_files": ["src/components/dashboard.tsx"],
    }


def test_nine_tasks_escalates_but_can_still_pass(repo_id, monkeypatch):
    from src.planning import escalation_for
    monkeypatch.delenv("MAX_PLAN_TASKS", raising=False)
    out = spec_plan(**_nine_tasks())
    assert "9 tasks" in escalation_for(out)


def test_env_override_lifts_the_threshold(repo_id, monkeypatch):
    from src.planning import escalation_for
    monkeypatch.setenv("MAX_PLAN_TASKS", "20")
    assert escalation_for(spec_plan(**_nine_tasks())) is None


def test_too_many_touchable_files_escalates(monkeypatch):
    from src.planning import escalation_for
    monkeypatch.delenv("MAX_TOUCHABLE_FILES", raising=False)
    out = spec_plan(spec__touchable_files=[f"src/components/f{i}.tsx" for i in range(13)])
    assert "MAX_TOUCHABLE_FILES=12" in escalation_for(out)


# ------------------------------------------------------------- repair loop


class FakeGateway:
    """Canned payloads consumed in order, the last one repeating. Nothing else
    to stub: both pipelines are handed their pack, so Stage A never runs."""

    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls: list[dict] = []

    def complete_json(self, *, schema, purpose, **kwargs):
        self.calls.append({"purpose": purpose, **kwargs})
        return schema.model_validate(self.payloads[min(len(self.calls) - 1,
                                                        len(self.payloads) - 1)])


def _payload(**overrides) -> dict:
    return spec_plan(**overrides).plan.model_dump()


BROKEN = _payload(plan__tasks__1__criterion_refs=[])          # orphan task t2

REQUEST = "Add a disabled state to Button"


def test_repair_loop_fixes_and_reports(repo_id, pack):
    gw = FakeGateway([BROKEN, _payload()])
    result = plan_pipeline(REQUEST, repo_id, spec_only(), gw, pack=pack)

    assert result.repair_attempts == 1
    assert result.report.passed
    assert result.escalation is None

    repair_call = next(c for c in gw.calls if c["purpose"] == "plan_repair")
    assert "REPAIR" in repair_call["user"]
    assert "traceability" in repair_call["user"]
    assert "references no existing criterion" in repair_call["user"]
    assert "Fix ONLY these issues" in repair_call["user"]
    assert "APPROVED SPEC" in repair_call["user"]       # the frozen spec is still in front of it
    # the model's own previous JSON is echoed back, without augmented edges
    assert '"from_task": "t1"' in repair_call["user"]


def test_always_invalid_returns_failed_result_without_raising(repo_id, pack):
    gw = FakeGateway([BROKEN])
    result = plan_pipeline(REQUEST, repo_id, spec_only(), gw, pack=pack, max_repairs=1)

    assert result.repair_attempts == 1
    assert not result.report.passed
    assert "traceability" in failed(result.report)
    assert sum(1 for c in gw.calls if c["purpose"].startswith("plan")) == 2


def test_no_repair_budget_means_one_generation(repo_id, pack):
    gw = FakeGateway([BROKEN])
    result = plan_pipeline(REQUEST, repo_id, spec_only(), gw, pack=pack, max_repairs=0)
    assert result.repair_attempts == 0
    assert not result.report.passed
    assert sum(1 for c in gw.calls if c["purpose"].startswith("plan")) == 1


def test_approved_spec_survives_the_repair_loop_verbatim(repo_id, pack):
    """The spec is an input at gate 2, not something the plan call can rewrite."""
    approved = spec_only()
    result = plan_pipeline(REQUEST, repo_id, approved, FakeGateway([BROKEN]), pack=pack)
    assert result.output.spec.model_dump_json() == approved.spec.model_dump_json()
    assert result.output.open_questions == approved.open_questions


# --------------------------------------------------- validate_spec + gate 1


def test_spec_repair_loop_fixes_and_reports(repo_id, pack):
    broken = spec_only(open_questions=[{"q": "Ship behind a flag?", "default": "  "}]).model_dump()
    gw = FakeGateway([broken, spec_only().model_dump()])
    result = spec_pipeline(REQUEST, repo_id, gw, pack=pack)

    assert result.repair_attempts == 1
    assert result.report.passed
    assert result.escalation is None
    assert [c["purpose"] for c in gw.calls] == ["spec", "spec_repair"]
    assert "has no default" in gw.calls[1]["user"]
    assert "Fix ONLY these issues" in gw.calls[1]["user"]


def test_spec_pipeline_escalates_on_touchable_files(repo_id, pack, monkeypatch):
    monkeypatch.delenv("MAX_TOUCHABLE_FILES", raising=False)
    payload = spec_only(spec__touchable_files=[f"src/components/f{i}.tsx" for i in range(13)])
    result = spec_pipeline(REQUEST, repo_id, FakeGateway([payload.model_dump()]), pack=pack)
    assert "MAX_TOUCHABLE_FILES=12" in result.escalation


def test_validate_spec_runs_only_the_three_spec_checks(repo_id):
    report = validate_spec(spec_only(), repo_id)
    assert report.passed, [c for c in report.checks if not c.passed]
    assert [c.name for c in report.checks] == [
        "path_validity", "criteria_wellformed", "scope_consistency",
    ]


def test_validate_spec_catches_spec_only_failures(repo_id):
    report = validate_spec(
        spec_only(spec__non_goals=["Will not modify src/components/dashboard.tsx because stable"],
                  spec__criteria__0__then="works"),
        repo_id,
    )
    assert failed(report) == {"criteria_wellformed", "scope_consistency"}
    assert "src/components/dashboard.tsx" in check(report, "scope_consistency").details[0]
