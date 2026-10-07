"""Stage A tests. Unit tests mock the gateway and index_query entirely;
the two integration tests use the indexed sample_repo with a fake gateway."""

from pathlib import Path

import pytest

from src import context as ctx
from src import index_query as iq
from src.index_query import FileHit, SymbolHit
from src.models import ExtractedEntities, PackedFile, RerankLabel, RerankResponse

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


# ------------------------------------------------------------------ fakes


def sym(name, path, sym_id=1, qualified=None, docstring=None, exported=True):
    return SymbolHit(
        id=sym_id, kind="function", name=name,
        qualified_name=qualified or f"{Path(path).stem}.{name}",
        start_line=1, end_line=2, signature=f"def {name}()",
        docstring=docstring, exported=exported, path=path,
    )


class FakeGateway:
    """Answers the two Stage A calls: entity extraction, then rerank."""

    def __init__(self, entities: ExtractedEntities, label="relevant", overrides=None):
        self.entities = entities
        self.label = label
        self.overrides = overrides or {}
        self.calls = []

    def complete_json(self, *, schema, user, **kwargs):
        self.calls.append(kwargs.get("purpose"))
        if schema is ExtractedEntities:
            return self.entities
        paths = [ln[4:].strip() for ln in user.splitlines() if ln.startswith("### ")]
        return RerankResponse(files=[
            RerankLabel(path=p, label=self.overrides.get(p, self.label), reason="because")
            for p in paths
        ])


@pytest.fixture
def stub_index(monkeypatch):
    """Every index_query function used by Stage A, stubbed to empty; each
    test overrides only the ones it cares about."""
    stubs = {
        "get_repo": lambda repo_id: {"id": repo_id, "root_path": None, "indexed_sha": "abc"},
        "get_file": lambda repo_id, path: None,
        "get_symbols_in_file": lambda repo_id, path: [],
        "find_symbols": lambda repo_id, name, **kw: [],
        "search_text": lambda repo_id, query, **kw: [],
        "neighbors": lambda repo_id, ids, **kw: [],
        "tests_of": lambda repo_id, names, **kw: [],
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(ctx.iq, name, fn)
    return monkeypatch


# ------------------------------------------------- 1. stack-frame parsing


PY_TRACEBACK = '''Traceback (most recent call last):
  File "/srv/app/src/auth/service.py", line 88, in login
    return validator.verify(token)
  File "/srv/app/src/auth/jwt.py", line 12, in verify
    return self._decode(token)
ValueError: signature has expired
'''

JS_STACK = '''TypeError: Cannot read properties of undefined (reading 'label')
    at Button (webpack-internal:///./src/components/dashboard.tsx:34:11)
    at renderWithHooks (webpack-internal:///./node_modules/react-dom/cjs/react-dom.development.js:16305:18)
    at eval (webpack://_N_E/./src/components/list.tsx:7:20)
    at src/lib/prisma.ts:9:3
'''


def test_stack_frames_python_and_js():
    frames = ctx.parse_stack_frames([PY_TRACEBACK, JS_STACK])
    got = [(f.path, f.line) for f in frames]

    # absolute traceback paths stay absolute here; resolve_indexed_path below
    # is what maps them onto repo-relative indexed paths
    assert ("/srv/app/src/auth/service.py", 88) in got
    assert ("/srv/app/src/auth/jwt.py", 12) in got
    # webpack-internal:/// and ./ stripped, webpack:// build segment stripped
    assert ("src/components/dashboard.tsx", 34) in got
    assert ("src/components/list.tsx", 7) in got
    assert ("src/lib/prisma.ts", 9) in got
    # node_modules frames never enter the pack
    assert not any("node_modules" in p for p, _ in got)


def test_resolve_indexed_path_strips_absolute_prefix(stub_index):
    indexed = {"src/auth/jwt.py": "content"}
    stub_index.setattr(ctx.iq, "get_file", lambda repo_id, path: indexed.get(path))

    assert ctx.resolve_indexed_path(1, "/srv/app/src/auth/jwt.py") == "src/auth/jwt.py"
    assert ctx.resolve_indexed_path(1, "/srv/app/src/auth/missing.py") is None


def test_fts_query_keeps_content_words_and_ors_them():
    q = ctx.fts_query("Please fix the bug where the invoice total is wrong")
    assert " or " in q
    assert "invoice" in q and "total" in q
    assert "the" not in q.split(" or ")
    assert ctx.fts_query("the and for") == ""


# ------------------------------------------------- 2. intersection ranking


def test_intersection_beats_raw_score(stub_index):
    """src/x.py is hit by 3 distinct entities with weak scores; src/y.py by
    one entity with the top score. Intersection wins."""
    stub_index.setattr(ctx.iq, "find_symbols", lambda repo_id, name, **kw: (
        [] if not kw.get("fuzzy") else [sym(name, "src/x.py")]
    ))
    stub_index.setattr(ctx.iq, "search_text", lambda repo_id, query, **kw: [
        FileHit(path="src/y.py", score=1.0, snippet=""),
        FileHit(path="src/x.py", score=0.05, snippet=""),
    ])

    entities = ExtractedEntities(symbols=["Alpha", "Beta"], request_type="feature")
    cands, _ = ctx.retrieve(1, "alpha beta checkout totals", entities, [])

    assert len(cands["src/x.py"].keys) == 3
    assert len(cands["src/y.py"].keys) == 1
    assert cands["src/y.py"].score > cands["src/x.py"].score  # higher raw score
    assert ctx.rank_seeds(cands, [])[0] == "src/x.py"         # still outranked


def test_stack_frame_files_are_always_seeds(stub_index):
    cands = {f"src/f{i}.py": ctx._Candidate(path=f"src/f{i}.py", keys={("s", i)}, score=99.0)
             for i in range(20)}
    seeds = ctx.rank_seeds(cands, ["src/boom.py"])
    assert seeds[0] == "src/boom.py"
    assert len(seeds) == ctx.MAX_SEEDS


# ------------------------------------------------- 3. common-name guard


def test_common_name_without_domain_overlap_is_dropped(stub_index):
    many = [sym("get", f"src/mod{i}/thing.py", sym_id=i) for i in range(12)]
    stub_index.setattr(ctx.iq, "find_symbols", lambda repo_id, name, **kw: many)

    entities = ExtractedEntities(symbols=["get"], domains=["billing"], request_type="bug")
    cands, stats = ctx.retrieve(1, "", entities, [])
    assert cands == {}
    assert stats["dropped_common_names"] == ["get"]


def test_common_name_with_domain_overlap_is_narrowed(stub_index):
    many = [sym("get", f"src/mod{i}/thing.py", sym_id=i) for i in range(12)]
    many.append(sym("get", "src/billing/invoice.py", sym_id=99))
    stub_index.setattr(ctx.iq, "find_symbols", lambda repo_id, name, **kw: many)

    entities = ExtractedEntities(symbols=["get"], domains=["billing"], request_type="bug")
    cands, stats = ctx.retrieve(1, "", entities, [])
    assert set(cands) == {"src/billing/invoice.py"}
    assert stats["dropped_common_names"] == []


# ------------------------------------------------- 4. budget packing


def test_over_budget_demotes_largest_tier1_but_never_the_stack_frame(stub_index):
    bodies = {
        "src/boom.py": "b" * 4000,      # stack frame: protected at tier 1
        "src/huge.py": "h" * 20000,     # critical, largest -> demoted first
        "src/mid.py": "m" * 5000,       # critical
    }
    stub_index.setattr(ctx.iq, "get_file", lambda repo_id, path: bodies[path])
    stub_index.setattr(ctx.iq, "get_symbols_in_file", lambda repo_id, path: [
        sym("f", path, docstring="doc")
    ])

    labeled = [("src/boom.py", "critical", "traceback"),
               ("src/huge.py", "critical", "big"),
               ("src/mid.py", "critical", "mid")]
    packed, stats = ctx.pack(1, labeled, {"src/boom.py"}, budget=3000)
    tiers = {f.path: f.tier for f in packed}

    assert tiers["src/boom.py"] == 1
    assert tiers["src/huge.py"] == 2
    assert stats["demotions"] >= 1
    # nothing truncated: the tier-1 file carries its whole body
    boom = next(f for f in packed if f.path == "src/boom.py")
    assert boom.content == bodies["src/boom.py"]


def test_packing_sheds_tier3_before_giving_up(stub_index):
    stub_index.setattr(ctx.iq, "get_file", lambda repo_id, path: "x" * 8000)
    stub_index.setattr(ctx.iq, "get_symbols_in_file", lambda repo_id, path: [])

    labeled = [("src/boom.py", "critical", "traceback")] + [
        (f"src/p{i}.py", "peripheral", "y" * 2000) for i in range(4)
    ]
    packed, stats = ctx.pack(1, labeled, {"src/boom.py"}, budget=2500)

    assert stats["dropped_for_budget"] > 0
    assert any(f.path == "src/boom.py" and f.tier == 1 for f in packed)


# ------------------------------------------------- 5. rerank protection


def test_seed_labeled_irrelevant_is_retained_as_peripheral(stub_index):
    stub_index.setattr(ctx.iq, "get_symbols_in_file", lambda repo_id, path: [])
    gateway = FakeGateway(ExtractedEntities(), label="irrelevant")

    kept, stats = ctx.rerank(
        gateway, 1, "request", ["src/seed.py", "src/other.py"], protected={"src/seed.py"},
    )
    assert [k[0] for k in kept] == ["src/seed.py"]
    assert kept[0][1] == "peripheral"
    assert stats["dropped_by_rerank"] == 1


# ------------------------------------------------- 6. bug vs feature weights


def test_request_type_flips_seed_order_on_a_tie(stub_index):
    """One file reached only by the symbol channel, one only by FTS, both with
    a single top-scoring hit: the request type decides which leads."""
    stub_index.setattr(ctx.iq, "find_symbols",
                       lambda repo_id, name, **kw: [sym("Widget", "src/auth/widget.py")])
    stub_index.setattr(ctx.iq, "search_text", lambda repo_id, query, **kw: [
        FileHit(path="src/docs/guide.py", score=1.0, snippet="")
    ])

    def order(request_type):
        entities = ExtractedEntities(symbols=["Widget"], request_type=request_type)
        cands, _ = ctx.retrieve(1, "text", entities, [])
        return ctx.rank_seeds(cands, [])

    assert order("bug")[0] == "src/auth/widget.py"
    assert order("feature")[0] == "src/docs/guide.py"


# ------------------------------------------------- integration (DB, fake LLM)


@pytest.fixture(scope="module")
def repo_id(db_url):
    from src.indexer.edges import build_edges
    from src.indexer.full_index import index_repo

    result = index_repo(str(FIXTURE_REPO))
    assert result.errors == []
    rid = iq.resolve_repo_id(str(FIXTURE_REPO))
    build_edges(rid)
    return rid


def test_bug_request_with_traceback_packs_jwt_at_tier1(repo_id):
    traceback = ('Traceback (most recent call last):\n'
                 '  File "/srv/app/src/auth/jwt.py", line 11, in verify\n'
                 '    return self._decode(token)\n'
                 'ValueError: signature has expired\n')
    entities = ExtractedEntities(
        symbols=["JwtValidator"], error_strings=["signature has expired"],
        domains=["auth"], request_type="bug",
    )
    pack = ctx.build_context(
        repo_id,
        "JwtValidator.verify raises 'signature has expired' for valid tokens",
        [traceback],
        gateway=FakeGateway(entities),
    )

    by_path = {f.path: f for f in pack.files}
    assert by_path["src/auth/jwt.py"].tier == 1
    assert "JwtValidator" in by_path["src/auth/jwt.py"].content
    assert "tests/test_jwt.py" in by_path
    assert pack.request_type == "bug"
    assert 0 < pack.token_count <= ctx.TOKEN_BUDGET
    assert pack.stats["stack_frames_in_index"] == 1
    assert all(isinstance(f, PackedFile) for f in pack.files)


def test_feature_request_surfaces_component_via_text_channels(repo_id):
    entities = ExtractedEntities(domains=["button", "component"], request_type="feature")
    pack = ctx.build_context(
        repo_id,
        "add a variant prop to the button component",
        gateway=FakeGateway(entities),
    )

    assert "src/components/dashboard.tsx" in {f.path for f in pack.files}
    assert pack.stats["seeds_per_channel"]["fts"] + pack.stats["seeds_per_channel"]["domain"] > 0
    assert pack.token_count <= ctx.TOKEN_BUDGET
