"""index_query tests against a fully indexed sample_repo."""

from pathlib import Path

import pytest

from src import index_query as iq  # module import: a bare `tests_of` import
# would get collected by pytest as a test function (name starts with "test")
from src.indexer.full_index import index_repo

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="module")
def repo_id(db_url):
    result = index_repo(str(FIXTURE_REPO))
    assert result.errors == []
    return iq.resolve_repo_id(str(FIXTURE_REPO))


def test_find_symbols_exact(repo_id):
    hits = iq.find_symbols(repo_id, "Button")
    assert any(
        h.qualified_name == "components.dashboard.Button"
        and h.kind == "function"
        and h.path == "src/components/dashboard.tsx"
        for h in hits
    )


def test_find_symbols_fuzzy(repo_id):
    hits = iq.find_symbols(repo_id, "valid", fuzzy=True)
    assert any(h.name == "JwtValidator" for h in hits)


def test_find_symbols_kind_filter(repo_id):
    hits = iq.find_symbols(repo_id, "Button", fuzzy=True, kind="interface")
    names = {h.name for h in hits}
    assert "ButtonProps" in names
    assert "Button" not in names


def test_search_text_fts(repo_id):
    hits = iq.search_text(repo_id, "Validates JWT", mode="fts")
    assert any(
        h.path == "src/auth/jwt.py" and "Validates JWT" in h.snippet for h in hits
    )


def test_search_text_trigram(repo_id):
    hits = iq.search_text(repo_id, "z.infer<typeof userSchema>", mode="trigram")
    assert any(h.path == "src/components/dashboard.tsx" for h in hits)


def test_get_symbols_in_file(repo_id):
    syms = iq.get_symbols_in_file(repo_id, "src/components/dashboard.tsx")
    assert len(syms) >= 8
    lines = [s.start_line for s in syms]
    assert lines == sorted(lines)


def test_get_file(repo_id):
    content = iq.get_file(repo_id, "src/auth/jwt.py")
    assert content is not None
    assert "JwtValidator" in content
    assert iq.get_file(repo_id, "no/such/file.py") is None


def test_tests_of_ts(repo_id):
    hits = iq.tests_of(repo_id, ["Button"])
    assert any(h.path == "src/components/__tests__/dashboard.test.tsx" for h in hits)


def test_tests_of_py(repo_id):
    hits = iq.tests_of(repo_id, ["JwtValidator"])
    assert any(h.path == "tests/test_jwt.py" for h in hits)
