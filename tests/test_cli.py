"""Interactive CLI, driven by scripted typing. Real project/repo registration
and real indexing of tests/fixtures/multi_repo; only the LLM routing step is
stubbed (its logic is covered in test_repo_router.py)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from src import cli, repo_router
from src.models import RepoSelection, ResolvedRepo

MULTI = Path(__file__).parent / "fixtures" / "multi_repo"


class Session:
    """Scripted stdin + captured stdout."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts, self.lines = [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def out(self, text):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def name():
    return f"cli-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def env(db_url, monkeypatch):
    # cli.run() loads a .env found above src/. A developer's real .env (real
    # DATABASE_URL / LLM_API_KEY) must never leak into tests, and would undo
    # the monkeypatch.delenv in the "missing key" tests.
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("LLM_API_KEY", "test")


@pytest.fixture
def fake_router(monkeypatch):
    calls = []

    def fake(project_id, text, **kw):
        calls.append((project_id, text))
        return RepoSelection(project_id=project_id, stats={"turns": 4, "tool_calls": 3}, repos=[
            ResolvedRepo(repo="storefront-web", role="impacted", reason="calls the endpoint", repo_id=2,
                         evidence=[{"path": "src/api/ordersClient.ts", "note": "fetch /api/orders"}]),
            ResolvedRepo(repo="orders-api", role="primary", reason="owns the endpoint", repo_id=1,
                         evidence=[{"path": "src/orders/routes.py", "note": "POST /api/orders"}]),
        ])

    monkeypatch.setattr(repo_router, "select_repos", fake)
    return calls


def test_fully_interactive_flow(fake_router):
    n = name()
    s = Session(n, str(MULTI / "orders-api"), str(MULTI / "storefront-web"), "",   # name, 2 repos, done
                "add a coupon field", "")                                            # one request, quit
    assert cli.run([], s.ask, s.out) == 0
    assert f"Created project {n!r}." in s.text
    assert "indexing" in s.text and "symbols" in s.text          # really indexed
    assert [t for _, t in fake_router] == ["add a coupon field"]
    # primary is printed before impacted, with reason and evidence
    assert s.text.index("PRIMARY   orders-api") < s.text.index("IMPACTED  storefront-web")
    assert "evidence: src/orders/routes.py - POST /api/orders" in s.text
    assert "4 model turns, 3 lookups" in s.text


def test_flags_run_a_single_request_non_interactively(fake_router):
    s = Session()   # any prompt would raise IndexError: nothing may be asked
    code = cli.run([name(), "-r", str(MULTI / "orders-api"), "-q", "add coupons"], s.ask, s.out)
    assert code == 0 and s.prompts == []
    assert [t for _, t in fake_router] == ["add coupons"]


def test_existing_project_goes_straight_to_the_request_without_asking_about_repos(fake_router):
    n = name()
    cli.run([n, "-r", str(MULTI / "orders-api"), "-q", "x"], Session().ask, lambda _: None)
    s = Session("add coupons", "")              # only the request prompt may ask anything
    assert cli.run([n], s.ask, s.out) == 0
    assert f"Project {n!r} already exists with 1 repo(s): orders-api" in s.text
    assert not any("Repos:" in p or p == "> " for p in s.prompts)
    assert fake_router[-1][1] == "add coupons"


def test_existing_project_can_gain_repos_with_the_flag(fake_router):
    n = name()
    cli.run([n, "-r", str(MULTI / "orders-api"), "-q", "x"], Session().ask, lambda _: None)
    assert cli.run([n, "-r", str(MULTI / "analytics-jobs"), "-q", "x"], Session().ask, lambda _: None) == 0
    from src.db import get_session
    from src import projects
    session = get_session()
    try:
        repos = projects.describe(session, projects.get_project_by_name(session, n))["repos"]
    finally:
        session.close()
    assert sorted(Path(r["root_path"]).name for r in repos) == ["analytics-jobs", "orders-api"]


def test_bad_repo_is_a_readable_error_not_a_traceback(fake_router):
    s = Session()
    code = cli.run([name(), "-r", "/no/such/dir", "-q", "x"], s.ask, s.out)
    assert code == 1 and "Error:" in s.text and "does not exist" in s.text
    assert fake_router == []


def test_new_project_without_repos_is_refused():
    s = Session(name(), "")        # name, then immediately an empty repo list
    assert cli.run([], s.ask, s.out) == 2
    assert "at least one repo" in s.text


def test_routing_errors_do_not_end_the_session(monkeypatch):
    def boom(*a, **kw):
        raise repo_router.RepoSelectionError("invalid after repair")

    monkeypatch.setattr(repo_router, "select_repos", boom)
    s = Session(name(), str(MULTI / "orders-api"), "", "first", "second", "")
    assert cli.run([], s.ask, s.out) == 0
    assert s.text.count("Error: invalid after repair") == 2     # both requests tried


def test_missing_llm_key_is_explained_after_indexing(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    s = Session()
    assert cli.run([name(), "-r", str(MULTI / "orders-api"), "-q", "x"], s.ask, s.out) == 2
    assert "LLM_API_KEY is not set" in s.text and "symbols" in s.text


def test_missing_database_url_is_explained(monkeypatch):
    monkeypatch.delenv("DATABASE_URL")
    s = Session()
    assert cli.run([name()], s.ask, s.out) == 2
    assert "DATABASE_URL is not set" in s.text


def test_pasted_lines_are_joined_into_one_request():
    spec = ["Reviewers see a badge.", "", "Requirements", "1. New factor", "2. Show breakdown"]
    ask = Session("\n".join(spec[:1])).ask
    got = cli._read_request(ask, pasted=lambda: spec[1:])
    assert got == "\n".join(spec)                      # blank line inside kept


def test_typed_line_is_the_whole_request_and_eof_or_empty_quits():
    assert cli._read_request(Session("add coupons").ask) == "add coupons"
    assert cli._read_request(Session("").ask) is None

    def eof(prompt):
        raise EOFError

    assert cli._read_request(eof) is None


def test_request_file_option(fake_router, tmp_path):
    f = tmp_path / "req.md"
    f.write_text("Line one\n\nLine three\n")
    s = Session()
    assert cli.run([name(), "-r", str(MULTI / "orders-api"), "-f", str(f)], s.ask, s.out) == 0
    assert [t for _, t in fake_router] == ["Line one\n\nLine three\n"]


# --- router settings come from the environment / .env -----------------------


def test_router_settings_default_and_come_from_dotenv(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    from src.config import get_settings

    monkeypatch.delenv("ROUTER_TIER", raising=False)
    monkeypatch.delenv("ROUTER_MAX_TURNS", raising=False)
    defaults = get_settings()
    assert (defaults.router_tier, defaults.router_max_turns) == ("cheap", 12)   # not forced to strong

    dotenv = tmp_path / ".env"
    dotenv.write_text("ROUTER_TIER=strong\nROUTER_MAX_TURNS=25\n")
    monkeypatch.setattr("os.environ", dict(__import__("os").environ))          # isolate the load
    load_dotenv(dotenv)
    configured = get_settings()
    assert (configured.router_tier, configured.router_max_turns) == ("strong", 25)


def test_a_real_environment_variable_beats_dotenv(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    from src.config import get_settings

    (tmp_path / ".env").write_text("ROUTER_TIER=strong\n")
    monkeypatch.setenv("ROUTER_TIER", "cheap")
    load_dotenv(tmp_path / ".env")        # same call the CLI makes: override=False
    assert get_settings().router_tier == "cheap"


# --- a request pasted at the repo prompt -----------------------------------

SENTENCE = "Implement workflow header dropdown functionality to enable customizable configuration."


def test_request_pasted_at_the_repo_prompt_is_offered_as_the_request(fake_router):
    s = Session(name(), SENTENCE, "y", "")        # name, request-by-mistake, yes, quit
    # no repos were given, so the project can't be created, but the sentence was NOT treated as a repo
    assert cli.run([], s.ask, s.out) == 2
    assert "That looks like a feature request, not a repo URL or path." in s.text
    assert "neither an absolute path nor a git URL" not in s.text


def test_sentence_at_the_repo_prompt_is_routed_after_the_repos_are_set_up(fake_router):
    s = Session(name(), str(MULTI / "orders-api"), SENTENCE, "", "")
    s.answers.insert(3, "y")                      # Use it as your feature request? -> y
    assert cli.run([], s.ask, s.out) == 0
    assert [t for _, t in fake_router] == [SENTENCE]


def test_declining_keeps_reading_repos(fake_router):
    s = Session(name(), SENTENCE, "n", str(MULTI / "orders-api"), "", "")
    assert cli.run([], s.ask, s.out) == 0
    assert fake_router == []                      # nothing routed; repo was still added


def test_a_mistyped_repo_is_reported_immediately_and_the_prompt_continues(fake_router):
    s = Session(name(), "orderz-api", str(MULTI / "orders-api"), "", "")
    assert cli.run([], s.ask, s.out) == 0
    assert "not added:" in s.text and "orderz-api" in s.text
    assert "Created project" in s.text


def test_multiline_paste_at_the_repo_prompt_becomes_one_request():
    lines = ["Reviewers see a badge but can't tell why.", "Requirements", "1. New factor"]
    asked = iter([lines[0], "y"])
    out = []
    repos, request = cli._read_repos(lambda p: next(asked), out.append, pasted=lambda: lines[1:])
    assert repos == [] and request == "\n".join(lines)


def test_trace_flag_is_passed_through_to_the_router(monkeypatch):
    seen = {}

    def fake(project_id, text, **kw):
        seen.update(kw)
        return RepoSelection(project_id=project_id, repos=[], stats={})

    monkeypatch.setattr(repo_router, "select_repos", fake)
    out = Session()
    cli.run([name(), "-r", str(MULTI / "orders-api"), "-q", "x", "--trace"], out.ask, out.out)
    assert seen["trace"] is not None
    seen.clear()
    cli.run([name(), "-r", str(MULTI / "orders-api"), "-q", "x"], out.ask, out.out)
    assert seen["trace"] is None


def test_left_out_repos_are_shown_with_their_reasons():
    from src.models import RuledOutRepo
    sel = RepoSelection(project_id=1, stats={"turns": 2, "tool_calls": 1}, repos=[
        ResolvedRepo(repo="orders-api", role="primary", reason="owns it", repo_id=1,
                     evidence=[{"path": "src/orders/routes.py", "note": "POST /api/orders"}])],
        ruled_out=[RuledOutRepo(repo="harbor-billing", repo_id=2, reason="reads orders.total only; searched 'orders'")])
    text = cli.format_selection(sel)
    assert "LEFT OUT  harbor-billing: reads orders.total only; searched 'orders'" in text
    assert text.index("PRIMARY") < text.index("LEFT OUT")
