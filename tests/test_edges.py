"""Tests for src.indexer.edges: import/call/inherits-implements resolution.

DB tests skip cleanly without TEST_DATABASE_URL (same pattern as full_index/
index_query tests) via the shared `db_url` fixture in conftest.py.
"""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from src import index_query as iq
from src.db import get_session
from src.indexer.edges import build_edges
from src.indexer.full_index import index_repo
from src.models import File, Symbol, SymbolEdge

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def tmp_repo(tmp_path):
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    return dest


@pytest.fixture
def repo(db_url, tmp_repo):
    """Index + build edges once per test; returns (repo_id, EdgeStats)."""
    result = index_repo(str(tmp_repo))
    assert result.errors == []
    repo_id = iq.resolve_repo_id(str(tmp_repo))
    stats = build_edges(repo_id)
    return repo_id, stats


def _sym_id(repo_id, qualified_name):
    session = get_session()
    try:
        sym = session.execute(
            select(Symbol).join(File).where(
                File.repo_id == repo_id, Symbol.qualified_name == qualified_name
            )
        ).scalar_one()
        return sym.id
    finally:
        session.close()


def _all_edges(repo_id):
    """[(from_qname, to_qname, kind, confidence), ...] for the whole repo."""
    session = get_session()
    try:
        qnames = {
            s.id: s.qualified_name
            for s in session.execute(
                select(Symbol).join(File).where(File.repo_id == repo_id)
            ).scalars()
        }
        rows = session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id)
        ).scalars().all()
        return [
            (qnames[e.from_symbol], qnames[e.to_symbol], e.kind, e.confidence)
            for e in rows
        ]
    finally:
        session.close()


def _edge_count(repo_id):
    session = get_session()
    try:
        return len(session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id)
        ).scalars().all())
    finally:
        session.close()


# ---------------------------------------------------------------- 1. TS relative import


def test_ts_relative_import_and_call_resolve(repo):
    repo_id, _ = repo
    edges = _all_edges(repo_id)
    assert (
        "components.list.renderList", "components.dashboard.Button",
        "imports", "resolved",
    ) in edges
    assert (
        "components.list.renderList", "components.dashboard.Button",
        "calls", "resolved",
    ) in edges


# ---------------------------------------------------------------- 2. TS alias + bare external


def test_ts_alias_resolves_and_bare_specifier_is_external(repo):
    repo_id, _ = repo
    imports = iq.imports_of_file(repo_id, "src/components/dashboard.tsx")
    by_spec = {i["raw_specifier"]: i for i in imports}

    assert by_spec["@/lib/prisma"]["imported_path"] == "src/lib/prisma.ts"
    assert by_spec["zod"]["imported_path"] is None

    edges = _all_edges(repo_id)
    assert any(
        to == "lib.prisma.prisma" and kind == "imports" and conf == "resolved"
        for _frm, to, kind, conf in edges
    )
    # no symbol anywhere is named "z" (zod's imported name), so there is
    # nothing a "z" edge could even target — confirm no symbol edge cites it
    assert not any(to.endswith(".z") for _frm, to, _kind, _conf in edges)


# ---------------------------------------------------------------- 3. Python import + attribute call


def test_python_import_and_attribute_call_resolve(repo):
    repo_id, _ = repo
    edges = _all_edges(repo_id)
    assert any(
        to == "auth.jwt.JwtValidator" and kind == "imports" and conf == "resolved"
        for frm, to, kind, conf in edges if frm.startswith("auth.service.")
    )
    assert (
        "auth.service.JwtValidatorPlus.verify_request", "auth.jwt.JwtValidator.verify",
        "calls", "resolved",
    ) in edges


# ---------------------------------------------------------------- 4. inherits, both languages


def test_inherits_resolved_both_languages(repo):
    repo_id, _ = repo
    edges = _all_edges(repo_id)
    assert (
        "components.list.UserList", "components.list.BaseList",
        "inherits", "resolved",
    ) in edges
    assert (
        "auth.service.JwtValidatorPlus", "auth.jwt.JwtValidator",
        "inherits", "resolved",
    ) in edges


# ---------------------------------------------------------------- 5. ambiguous bare-name call


def test_ambiguous_bare_call_is_heuristic_never_resolved(repo):
    repo_id, _ = repo
    edges = _all_edges(repo_id)
    calls = [
        (frm, to, conf) for frm, to, kind, conf in edges
        if frm == "auth.service.ambiguous_caller" and kind == "calls"
    ]
    targets = {to for _frm, to, _conf in calls}
    assert targets == {"auth.helpers.parse_claims", "auth.token_utils.parse_claims"}
    assert all(conf == "heuristic" for _frm, _to, conf in calls)


# ---------------------------------------------------------------- 6. neighbors()


def test_neighbors_callers_and_confidence_filter(repo):
    repo_id, _ = repo
    button_id = _sym_id(repo_id, "components.dashboard.Button")

    callers = iq.neighbors(repo_id, [button_id], direction="callers")
    assert any(h.qualified_name == "components.list.renderList" for h in callers)

    claims_a = _sym_id(repo_id, "auth.helpers.parse_claims")
    claims_b = _sym_id(repo_id, "auth.token_utils.parse_claims")
    heuristic_callers = iq.neighbors(
        repo_id, [claims_a, claims_b], direction="callers", min_confidence="heuristic",
    )
    assert any(h.qualified_name == "auth.service.ambiguous_caller" for h in heuristic_callers)

    resolved_only = iq.neighbors(
        repo_id, [claims_a, claims_b], direction="callers", min_confidence="resolved",
    )
    assert not any(h.qualified_name == "auth.service.ambiguous_caller" for h in resolved_only)


# ---------------------------------------------------------------- 7. idempotency


def test_build_edges_idempotent(repo):
    repo_id, _ = repo
    before = _edge_count(repo_id)
    build_edges(repo_id)
    after = _edge_count(repo_id)
    assert before == after
    assert before > 0


# ---------------------------------------------------------------- 8. unresolvable import


def test_unresolvable_import_no_crash_recorded_in_stats(repo):
    repo_id, stats = repo
    assert stats.imports_unresolved >= 2  # list.tsx's Ghost + service.py's Thing

    ts_imports = iq.imports_of_file(repo_id, "src/components/list.tsx")
    ghost = next(i for i in ts_imports if i["raw_specifier"] == "./does-not-exist")
    assert ghost["imported_path"] is None

    py_imports = iq.imports_of_file(repo_id, "src/auth/service.py")
    missing = next(i for i in py_imports if i["raw_specifier"] == "auth.does_not_exist")
    assert missing["imported_path"] is None

    edges = _all_edges(repo_id)
    assert not any(to.endswith(".Ghost") or to.endswith(".Thing") for _frm, to, _kind, _conf in edges)
