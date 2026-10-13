"""The router prompt has a feature part and a bug part. These pin the structure
(not the wording), so one kind of request can't be changed by editing the other."""

from pathlib import Path

PROMPT = (Path(__file__).resolve().parent.parent / "src" / "prompts" / "repo_router.md").read_text()


def section(title: str) -> str:
    """Text of the `## title` section (a heading at the start of a line)."""
    start = PROMPT.index(f"\n## {title}\n") + 1
    nxt = PROMPT.find("\n## ", start + 1)
    return PROMPT[start:] if nxt == -1 else PROMPT[start:nxt]


def test_sections_exist_in_order():
    heads = [ln for ln in PROMPT.splitlines() if ln.startswith("## ")]
    assert heads == ["## Two kinds of request", "## What to find", "## How to work",
                     "## How to work on a bug report", "## Rules", "## Final answer"]


def test_feature_instructions_do_not_mention_bug_handling():
    feature = section("What to find") + section("How to work")
    for word in ("root cause", "stack", "symptom", "the defect"):
        assert word not in feature.lower()
    # the feature steps are all still there
    assert "Check sibling apps" in feature and "decide EVERY repo" in feature
    assert "must be modified" in feature        # the primary rule for features


def test_bug_section_covers_the_bug_specific_ground():
    bug = section("How to work on a bug report").lower()
    for needle in ("root cause", "files named in the report", "trace backward", "same defect elsewhere",
                   "primary", "impacted", "decide every repo", "defective code"):
        assert needle in bug, needle


def test_the_type_can_be_stated_by_the_user():
    assert "## Request type" in section("Two kinds of request")


def test_shared_rules_apply_to_both_kinds():
    assert "apply to both" in section("Two kinds of request")
    rules = section("Rules")
    assert "ONLY by their number" in rules and "`ruled_out`" in rules
