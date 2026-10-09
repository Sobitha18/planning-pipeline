"""Repo router tests. No real LLM: the gateway is faked, but the tools run
against a real index of tests/fixtures/multi_repo (DB-backed tests skip
without TEST_DATABASE_URL, same as the rest of the suite).

Evidence is cited by file NUMBER (F12), resolved to a real path by code, so a
model can never put a mistyped path into a result: that is tested explicitly."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import index_query as iq
from src import repo_router as rr
from src.indexer.edges import build_edges
from src.indexer.full_index import index_repo
from src.llm import ToolLoopResult
from src.models import AgentAnswer, Project, ProjectRepo

FIXTURES = Path(__file__).parent / "fixtures" / "multi_repo"
REPO_DIRS = ["orders-api", "storefront-web", "inventory-service", "analytics-jobs"]


# ------------------------------------------------------------ pure (no DB)


def reg_with(*files):
    """Registry pre-loaded with (repo, path) pairs, in order: F1, F2, ..."""
    registry = rr.EvidenceRegistry()
    for repo, path in files:
        registry.ref(repo, path)
    return registry


def sel(repo, role="primary", files=("F1",)):
    return {"repo": repo, "role": role, "reason": "r",
            "evidence": [{"file": f, "note": "n"} for f in files]}


BY_NAME = {"api": {"id": 1}, "web": {"id": 2}}


def ans(*repos, ruled="rest"):
    """An AgentAnswer. By default every project repo not in `repos` is ruled
    out (so a test only has to say what it is about); pass ruled=[] to leave
    repos undecided, or ruled=[{"repo":..,"reason":..}] explicitly."""
    if ruled == "rest":
        chosen = {r["repo"] for r in repos}
        ruled = [{"repo": n, "reason": "no link found"} for n in BY_NAME if n not in chosen]
    return AgentAnswer.model_validate({"repos": list(repos), "ruled_out": ruled})

REGISTRY_FILES = [("api", "a.py"), ("web", "w.ts"), ("api", "b.py")]    # F1, F2, F3


def test_repo_names_use_basename_and_disambiguate_clashes():
    repos = [
        {"id": 1, "root_path": "/x/app"}, {"id": 2, "root_path": "/y/app"},
        {"id": 3, "root_path": "/z/web"},
    ]
    assert sorted(rr.repo_names(repos)) == ["app-1", "app-2", "web"]


def test_registry_numbers_each_file_once_and_looks_up_case_insensitively():
    r = rr.EvidenceRegistry()
    assert r.ref("api", "a.py") == "F1"
    assert r.ref("web", "a.py") == "F2"          # same path, different repo: different file
    assert r.ref("api", "a.py") == "F1"          # stable
    assert r.get("f2") == ("web", "a.py") and r.get(" F1 ") == ("api", "a.py")
    assert r.get("F9") is None
    assert "F1  api  a.py" in r.listing() and len(r) == 2


def test_validate_ok():
    reg = reg_with(*REGISTRY_FILES)
    assert rr.validate_answer(ans(sel("api", files=("F1", "F3")), sel("web", "impacted", ("F2",))), BY_NAME, reg) == []


def test_validate_unknown_repo():
    errs = rr.validate_answer(ans(sel("api"), sel("ghost", "impacted")), BY_NAME, reg_with(*REGISTRY_FILES))
    assert any("'ghost' is not in this project" in e for e in errs)


def test_a_file_number_no_tool_ever_showed_is_rejected():
    errs = rr.validate_answer(ans(sel("api", files=("F1", "F99"))), BY_NAME, reg_with(*REGISTRY_FILES))
    assert len(errs) == 1 and "'F99' was never shown by a tool" in errs[0]


def test_a_path_typed_instead_of_a_number_is_rejected():
    """The failure that motivated numbering: a model-typed (half-remembered) path."""
    typed = "src/features/work-queue/services/get-member-work-items.ts"
    errs = rr.validate_answer(ans(sel("api", files=("F1", typed))), BY_NAME, reg_with(*REGISTRY_FILES))
    assert len(errs) == 1 and "was never shown by a tool" in errs[0]


def test_evidence_must_come_from_the_repo_being_described():
    errs = rr.validate_answer(ans(sel("api", files=("F1", "F2"))), BY_NAME, reg_with(*REGISTRY_FILES))
    assert errs == ["file F2 is in repo 'web', not 'api'; cite files from the repo you are describing"]


def test_validate_needs_a_primary_and_no_duplicates():
    reg = reg_with(*REGISTRY_FILES)
    assert any("no primary" in e for e in rr.validate_answer(ans(sel("web", "impacted", ("F2",))), BY_NAME, reg))
    assert any("more than once" in e for e in rr.validate_answer(ans(sel("api"), sel("api")), BY_NAME, reg))


def test_every_repo_must_be_decided_a_silently_dropped_repo_is_an_error():
    """The harbor-mobile case: a repo is simply left out of the answer."""
    reg = reg_with(*REGISTRY_FILES)
    errs = rr.validate_answer(ans(sel("api"), ruled=[]), BY_NAME, reg)
    assert len(errs) == 1 and "these repos were not decided: web" in errs[0]


def test_ruled_out_must_be_valid_and_not_overlap_the_selection():
    reg = reg_with(*REGISTRY_FILES)
    errs = rr.validate_answer(ans(sel("api"), ruled=[{"repo": "web", "reason": "x"}, {"repo": "ghost", "reason": "x"}]), BY_NAME, reg)
    assert any("'ghost', which is not in this project" in e for e in errs)
    errs = rr.validate_answer(ans(sel("api"), ruled=[{"repo": "api", "reason": "x"}, {"repo": "web", "reason": "x"}]), BY_NAME, reg)
    assert any("'api' is both selected and in ruled_out" in e for e in errs)
    errs = rr.validate_answer(ans(sel("api"), ruled=[{"repo": "web", "reason": "x"}, {"repo": "web", "reason": "y"}]), BY_NAME, reg)
    assert any("'web' is listed more than once in ruled_out" in e for e in errs)


def test_evidence_is_required_by_schema():
    with pytest.raises(Exception):
        ans({"repo": "api", "role": "primary", "reason": "r", "evidence": []})


def test_prune_keeps_valid_citations_and_drops_repos_left_with_none():
    reg = reg_with(*REGISTRY_FILES)
    answer = ans(sel("api", files=("F1", "F99")), sel("web", "impacted", ("F1",)))   # web cites api's file
    pruned, dropped = rr.prune_invalid_citations(answer, BY_NAME, reg)
    assert [(s.repo, [e.file for e in s.evidence]) for s in pruned.repos] == [("api", ["F1"])]
    assert {"repo": "api", "file": "F99"} in dropped
    assert any(d["repo"] == "web" and d["file"] is None for d in dropped)
    # a repo that lost all its evidence is ruled out, so the answer stays complete
    assert [r.repo for r in pruned.ruled_out if "could not be verified" in r.reason] == ["web"]
    assert rr.validate_answer(pruned, BY_NAME, reg) == []


def test_resolve_writes_real_paths_primary_first():
    reg = reg_with(*REGISTRY_FILES)
    answer = ans(sel("web", "impacted", ("F2",)), sel("api", files=("F3", "F1")))
    out = rr.resolve_answer(answer, BY_NAME, reg)
    assert [(r.repo, r.role, r.repo_id) for r in out] == [("api", "primary", 1), ("web", "impacted", 2)]
    assert [e.path for e in out[0].evidence] == ["b.py", "a.py"]      # paths come from the registry
    assert [e.path for e in out[1].evidence] == ["w.ts"]
    ruled = rr.resolve_ruled_out(ans(sel("api"), ruled=[{"repo": "web", "reason": "not related"}]), BY_NAME)
    assert [(r.repo, r.repo_id, r.reason) for r in ruled] == [("web", 2, "not related")]


# ------------------------------------------------------- DB-backed fixtures


@pytest.fixture(scope="module")
def project_id(db_url):
    from src.db import get_session
    from src.models import Repo
    from sqlalchemy import select

    for d in REPO_DIRS:
        assert index_repo(str(FIXTURES / d)).errors == []
    session = get_session()
    try:
        ids = [
            session.execute(select(Repo.id).where(Repo.root_path == str((FIXTURES / d).resolve()))).scalar_one()
            for d in REPO_DIRS
        ]
        for repo_id in ids:
            build_edges(repo_id)          # imports/importers data, as the CLI and API do after indexing
        project = Project(name="shop")
        project.repos = [ProjectRepo(repo_id=i) for i in ids]
        session.add(project)
        session.commit()
        return project.id
    finally:
        session.close()


@pytest.fixture
def tools(project_id):
    return rr.RouterTools(rr.repo_names(iq.get_project_repos(project_id)))


def fid(text, path):
    """The file number a tool result printed for `path`."""
    m = re.search(rf"\[(F\d+)\] {re.escape(path)}\b", text)
    assert m, f"{path} not numbered in:\n{text}"
    return m.group(1)


def test_list_repos_profiles_show_manifest_readme_and_tables(tools):
    profiles = {p["name"]: p for p in json.loads(tools.call("list_repos", {}))}
    assert set(profiles) == set(REPO_DIRS)
    api = profiles["orders-api"]
    assert "pyproject.toml" in api["manifests"] and "HTTP API" in api["readme"]
    assert api["tables_defined"] == ["orders"]
    assert "tables_defined" not in profiles["inventory-service"]


def test_search_code_literal_groups_hits_by_repo_and_numbers_files(tools):
    text = tools.call("search_code", {"query": "/api/orders", "mode": "literal"})
    assert "## orders-api" in text and "## storefront-web" in text and "inventory-service" not in text
    api_id = fid(text, "src/orders/routes.py")
    assert tools.registry.get(api_id) == ("orders-api", "src/orders/routes.py")


def test_the_same_file_keeps_its_number_across_tools(tools):
    a = tools.call("search_code", {"query": "/api/orders", "repo": "orders-api", "mode": "literal"})
    b = tools.call("read_file", {"repo": "orders-api", "path": "src/orders/routes.py"})
    c = tools.call("file_outline", {"repo": "orders-api", "path": "src/orders/routes.py"})
    d = tools.call("list_files", {"repo": "orders-api"})
    ids = {fid(a, "src/orders/routes.py"), fid(b, "src/orders/routes.py"),
           fid(c, "src/orders/routes.py"), fid(d, "src/orders/routes.py")}
    assert len(ids) == 1


def test_search_code_can_be_scoped_to_one_repo(tools):
    text = tools.call("search_code", {"query": "/api/orders", "repo": "storefront-web", "mode": "literal"})
    assert "orders-api" not in text and "src/api/ordersClient.ts" in text


def test_find_tables_numbers_sql_and_orm_definitions(tools):
    text = tools.call("find_tables", {"name": "orders"})
    mine = [ln for ln in text.splitlines() if ln.startswith("orders-api:")]
    joined = " ".join(mine)
    assert "db/migrations/001_create_orders.sql" in joined and "src/orders/db_models.py" in joined
    fid(text, "db/migrations/001_create_orders.sql")
    assert not any(ln.startswith(("storefront-web", "analytics-jobs")) for ln in text.splitlines())


def test_importers_and_imports_number_the_files_they_show(tools):
    importers = tools.call("importers_of", {"repo": "storefront-web", "path": "src/api/ordersClient.ts"})
    fid(importers, "src/pages/Checkout.tsx")
    imports = tools.call("imports_of", {"repo": "storefront-web", "path": "src/pages/Checkout.tsx"})
    assert re.search(r"-> \[F\d+\] src/api/ordersClient", imports)


def test_tool_errors_are_readable_not_crashes(tools):
    with pytest.raises(ValueError, match="valid repos"):
        tools.call("list_files", {"repo": "nope"})
    with pytest.raises(ValueError, match="not an indexed file"):
        tools.call("read_file", {"repo": "orders-api", "path": "ghost.py"})
    with pytest.raises(ValueError, match="unknown tool"):
        tools.call("rm_rf", {})
    assert len(tools.registry) == 0           # nothing numbered for failed lookups


# ---------------------------------------------------- select_repos (fake LLM)


class FakeGateway:
    """Stands in for LLMGateway. run_tool_loop really executes the scripted
    tool calls through the router's handler (real tools, real index, real
    numbering); `final(results)` then builds the model's answer from what the
    tools printed, the way a model reads its own tool results."""

    def __init__(self, tool_calls, final, repair=None):
        self.tool_calls, self.final, self.repair = tool_calls, final, repair
        self.results, self.repair_calls, self.repair_user = [], 0, ""

    @staticmethod
    def _complete(answer):
        """Dict answers that don't mention `ruled_out` get every other repo
        ruled out, so a test only spells out what it is about."""
        if isinstance(answer, dict) and "ruled_out" not in answer:
            chosen = {r["repo"] for r in answer["repos"]}
            answer = {**answer, "ruled_out": [
                {"repo": d, "reason": "no link found by any search"} for d in REPO_DIRS if d not in chosen]}
        return answer

    def run_tool_loop(self, *, handler, tools, **kw):
        self.results = [handler(name, args) for name, args in self.tool_calls]
        text = self._complete(self.final("\n".join(self.results)))
        return ToolLoopResult(text=text if isinstance(text, str) else json.dumps(text),
                              turns=len(self.tool_calls) + 1,
                              tool_calls=[{"name": n} for n, _ in self.tool_calls])

    def complete_json(self, *, user, schema, **kw):
        self.repair_calls += 1
        self.repair_user = user
        reply = self._complete(self.repair(user))
        return schema.model_validate(reply)


def listing_id(repair_user, repo, path):
    """File number for (repo, path) from the repair prompt's citable-files list."""
    m = re.search(rf"^(F\d+)  {re.escape(repo)}  {re.escape(path)}$", repair_user, re.M)
    assert m, f"{repo}:{path} not in repair listing"
    return m.group(1)


def api_and_web_answer(results):
    return {"repos": [
        {"repo": "storefront-web", "role": "impacted", "reason": "calls the endpoint",
         "evidence": [{"file": fid(results, "src/api/ordersClient.ts"), "note": "fetch /api/orders"}]},
        {"repo": "orders-api", "role": "primary", "reason": "owns the endpoint",
         "evidence": [{"file": fid(results, "src/orders/routes.py"), "note": "POST /api/orders"}]},
    ]}


SEARCH_API = [("list_repos", {}), ("search_code", {"query": "/api/orders", "mode": "literal"})]


def test_select_repos_happy_path_paths_come_from_the_registry(project_id):
    gw = FakeGateway(SEARCH_API, api_and_web_answer)
    result = rr.select_repos(project_id, "add a coupon field to order creation", gateway=gw)
    assert [(r.repo, r.role) for r in result.repos] == [("orders-api", "primary"), ("storefront-web", "impacted")]
    assert [e.path for e in result.repos[0].evidence] == ["src/orders/routes.py"]
    assert [e.path for e in result.repos[1].evidence] == ["src/api/ordersClient.ts"]
    ids = {r["root_path"].rsplit("/", 1)[1]: r["id"] for r in iq.get_project_repos(project_id)}
    assert all(r.repo_id == ids[r.repo] for r in result.repos)
    assert result.stats["repaired"] is False and result.stats["dropped_evidence"] == []
    assert result.stats["files_numbered"] >= 2 and gw.repair_calls == 0


def test_select_repos_database_change_finds_schema_owner_and_reader(project_id):
    """A DB change: find_tables surfaces the schema owner, a literal search the
    repo that reads the table with raw SQL."""
    def final(results):
        return {"repos": [
            {"repo": "orders-api", "role": "primary", "reason": "defines orders table",
             "evidence": [{"file": fid(results, "db/migrations/001_create_orders.sql"), "note": "CREATE TABLE orders"}]},
            {"repo": "analytics-jobs", "role": "impacted", "reason": "selects from orders",
             "evidence": [{"file": fid(results, "src/reports/daily_revenue.py"), "note": "SELECT ... FROM orders"}]},
        ]}

    gw = FakeGateway([("find_tables", {"name": "orders"}), ("search_code", {"query": "orders", "mode": "literal"})], final)
    result = rr.select_repos(project_id, "add a discount_cents column to orders", gateway=gw)
    assert {r.repo for r in result.repos} == {"orders-api", "analytics-jobs"}
    assert result.repos[1].evidence[0].path == "src/reports/daily_revenue.py"


def test_a_model_typed_path_never_reaches_the_result(project_id):
    """Regression for the real failure: right filename, wrong directory. The
    model typed a path instead of a number, so the answer is rejected and the
    repair, handed the list of citable files, cites by number."""
    def final(results):
        return {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                           "evidence": [{"file": "src/wrong-dir/orders/routes.py", "note": "n"}]}]}

    def repair(user):
        return {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                           "evidence": [{"file": listing_id(user, "orders-api", "src/orders/routes.py"), "note": "n"}]}]}

    gw = FakeGateway(SEARCH_API, final, repair)
    result = rr.select_repos(project_id, "x", gateway=gw)
    assert gw.repair_calls == 1 and result.stats["repaired"] is True
    assert "'src/wrong-dir/orders/routes.py' was never shown by a tool" in gw.repair_user
    assert [e.path for e in result.repos[0].evidence] == ["src/orders/routes.py"]    # real path, written by code


def test_non_json_final_text_is_repaired(project_id):
    def repair(user):
        return {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                           "evidence": [{"file": listing_id(user, "orders-api", "src/orders/routes.py"), "note": "n"}]}]}

    gw = FakeGateway(SEARCH_API, lambda r: "I think it's orders-api", repair)
    assert rr.select_repos(project_id, "x", gateway=gw).stats["repaired"] is True


def test_citations_still_bad_after_repair_are_dropped_but_valid_ones_stay(project_id):
    def final(results):
        return {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                           "evidence": [{"file": "F999", "note": "n"}]}]}

    def repair(user):      # repair still cites a junk number alongside a good one
        good = listing_id(user, "orders-api", "src/orders/routes.py")
        return {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                           "evidence": [{"file": good, "note": "n"}, {"file": "F999", "note": "n"}]}]}

    result = rr.select_repos(project_id, "x", gateway=FakeGateway(SEARCH_API, final, repair))
    assert [e.path for e in result.repos[0].evidence] == ["src/orders/routes.py"]
    assert result.stats["dropped_evidence"] == [{"repo": "orders-api", "file": "F999"}]


def test_select_repos_raises_if_nothing_valid_survives(project_id):
    bad = {"repos": [{"repo": "orders-api", "role": "primary", "reason": "r",
                      "evidence": [{"file": "F999", "note": "n"}]}]}
    gw = FakeGateway(SEARCH_API, lambda r: bad, lambda user: bad)
    with pytest.raises(rr.RepoSelectionError, match="invalid after repair"):
        rr.select_repos(project_id, "x", gateway=gw)


def test_select_repos_empty_project_raises(db_url):
    with pytest.raises(rr.RepoSelectionError, match="no repos"):
        rr.select_repos(999999, "x", gateway=FakeGateway([], lambda r: "{}"))


# --- trace: every lookup the agent makes is reported -------------------------


def test_summarize_result_is_one_short_line():
    assert rr.summarize_result("## api (3 hits)\n- [F1] a\n\n## web (2 hits)\n- [F4] b") == "hits in api(3), web(2)"
    assert rr.summarize_result("no hits") == "no hits"
    assert rr.summarize_result("[F2] x.py\n1: a\n2: b").startswith("3 lines, first: [F2] x.py")
    assert rr.summarize_result("error: unknown repo") == "error: unknown repo"


def test_trace_reports_each_lookup_with_its_arguments_and_outcome(project_id):
    lines: list[str] = []
    gw = FakeGateway(SEARCH_API, api_and_web_answer)
    rr.select_repos(project_id, "x", gateway=gw, trace=lines.append)
    assert len(lines) == 2
    assert lines[0].lstrip().startswith("1. list_repos {}")
    assert lines[1].lstrip().startswith('2. search_code {"query": "/api/orders", "mode": "literal"}')
    assert "hits in orders-api(1), storefront-web(2)" in lines[1]


def test_trace_reports_tool_errors_and_does_not_swallow_them(project_id):
    lines: list[str] = []
    gw = FakeGateway([("list_files", {"repo": "nope"})], lambda r: "{}", repair=lambda u: (_ for _ in ()).throw(AssertionError))
    with pytest.raises(ValueError, match="valid repos"):      # the error still reaches the tool loop
        rr.select_repos(project_id, "x", gateway=gw, trace=lines.append)
    assert "ERROR: unknown repo 'nope'" in lines[0]


def test_no_trace_means_no_wrapping(project_id):
    gw = FakeGateway(SEARCH_API, api_and_web_answer)
    assert rr.select_repos(project_id, "x", gateway=gw).stats["tool_calls"] == 2


# --- every repo is decided: the silent-omission fix ---------------------------


def test_ruled_out_repos_are_returned_with_their_reasons(project_id):
    gw = FakeGateway(SEARCH_API, api_and_web_answer)
    result = rr.select_repos(project_id, "x", gateway=gw)
    assert sorted(r.repo for r in result.ruled_out) == ["analytics-jobs", "inventory-service"]
    assert all(r.reason and r.repo_id for r in result.ruled_out)


def test_a_repo_left_out_without_a_decision_is_repaired_not_dropped(project_id):
    """The harbor-mobile bug: the agent just never mentions a repo. Now that is
    rejected, the repair is told which repo is undecided, and it must decide."""
    def final(results):                      # analytics-jobs is never mentioned
        return {**api_and_web_answer(results),
                "ruled_out": [{"repo": "inventory-service", "reason": "unrelated"}]}

    def repair(user):
        return {"repos": [
            {"repo": "orders-api", "role": "primary", "reason": "r",
             "evidence": [{"file": listing_id(user, "orders-api", "src/orders/routes.py"), "note": "n"}]},
            {"repo": "analytics-jobs", "role": "impacted", "reason": "reads orders",
             "evidence": [{"file": listing_id(user, "analytics-jobs", "src/reports/daily_revenue.py"), "note": "n"}]},
        ], "ruled_out": [{"repo": "inventory-service", "reason": "unrelated"},
                         {"repo": "storefront-web", "reason": "no change needed"}]}

    calls = SEARCH_API + [("search_code", {"query": "orders", "mode": "literal"})]
    gw = FakeGateway(calls, final, repair)
    result = rr.select_repos(project_id, "x", gateway=gw)
    assert gw.repair_calls == 1
    assert "these repos were not decided: analytics-jobs" in gw.repair_user
    assert {r.repo for r in result.repos} == {"orders-api", "analytics-jobs"}
    assert result.stats["repaired"] is True
