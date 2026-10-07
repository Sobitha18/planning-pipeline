"""Indexer tests: DB-backed behavior (skipped without TEST_DATABASE_URL) plus
DB-free unit tests for the ignore rules."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db import get_session
from src.indexer import full_index
from src.indexer.full_index import index_repo
from src.indexer.ignore import should_index
from src.models import File, Repo, Symbol

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def tmp_repo(tmp_path):
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    return dest


def _repo(root):
    session = get_session()
    repo = session.execute(select(Repo).where(Repo.root_path == str(root))).scalar_one()
    session.close()
    return repo


def _symbol_qnames(repo_id):
    session = get_session()
    qnames = {
        s.qualified_name
        for s in session.execute(
            select(Symbol).join(File).where(File.repo_id == repo_id)
        ).scalars()
    }
    session.close()
    return qnames


# ---------------------------------------------------------------- DB tests


def test_index_repo_indexes_expected_files(db_url, tmp_repo):
    result = index_repo(str(tmp_repo))
    # jwt.py, dashboard.tsx, dashboard.test.tsx, test_jwt.py,
    # schema.prisma, prisma/schema.prisma (03b),
    # service.py, helpers.py, token_utils.py, list.tsx, prisma.ts (03, edge fixtures)
    assert result.files_indexed == 11
    assert result.errors == []
    assert result.symbols_count > 0

    repo = _repo(tmp_repo)
    assert repo.status == "ready"
    session = get_session()
    langs = {
        f.language for f in session.execute(select(File).where(File.repo_id == repo.id)).scalars()
    }
    paths = {
        f.path for f in session.execute(select(File).where(File.repo_id == repo.id)).scalars()
    }
    session.close()
    assert "python" in langs and "typescript" in langs
    assert "node_modules/junk.ts" not in paths
    assert "__pycache__/junk.py" not in paths
    assert "schema.prisma" in paths
    assert "prisma/schema.prisma" in paths


def test_symbols_queryable(db_url, tmp_repo):
    index_repo(str(tmp_repo))
    repo = _repo(tmp_repo)
    qnames = _symbol_qnames(repo.id)
    assert "auth.jwt.JwtValidator" in qnames
    assert "components.dashboard.Button" in qnames


def test_idempotent_reindex(db_url, tmp_repo):
    index_repo(str(tmp_repo))
    repo = _repo(tmp_repo)
    session = get_session()
    ids_before = {
        s.id for s in session.execute(
            select(Symbol).join(File).where(File.repo_id == repo.id)
        ).scalars()
    }
    session.close()

    result = index_repo(str(tmp_repo))
    assert result.files_indexed == 0
    assert result.files_skipped_unchanged == 11
    assert result.files_deleted == 0

    session = get_session()
    ids_after = {
        s.id for s in session.execute(
            select(Symbol).join(File).where(File.repo_id == repo.id)
        ).scalars()
    }
    session.close()
    assert ids_before == ids_after


def test_change_detection(db_url, tmp_repo):
    index_repo(str(tmp_repo))
    jwt = tmp_repo / "src" / "auth" / "jwt.py"
    jwt.write_text(jwt.read_text() + "\n\ndef new_func():\n    pass\n")

    result = index_repo(str(tmp_repo))
    assert result.files_indexed == 1
    assert result.files_skipped_unchanged == 10

    repo = _repo(tmp_repo)
    qnames = _symbol_qnames(repo.id)
    assert "auth.jwt.new_func" in qnames


def test_deletion(db_url, tmp_repo):
    index_repo(str(tmp_repo))
    (tmp_repo / "tests" / "test_jwt.py").unlink()

    result = index_repo(str(tmp_repo))
    assert result.files_deleted == 1

    repo = _repo(tmp_repo)
    session = get_session()
    paths = {
        f.path for f in session.execute(select(File).where(File.repo_id == repo.id)).scalars()
    }
    session.close()
    assert "tests/test_jwt.py" not in paths


def test_per_file_error_tolerance(db_url, tmp_repo, monkeypatch):
    real_extract = full_index.extract

    def flaky(path, content, repo_root=None):
        if path == "src/components/dashboard.tsx":
            raise ValueError("boom")
        return real_extract(path, content, repo_root=repo_root)

    monkeypatch.setattr(full_index, "extract", flaky)
    result = index_repo(str(tmp_repo))

    assert len(result.errors) == 1
    assert result.errors[0][0] == "src/components/dashboard.tsx"
    # everything except dashboard.tsx: see test_index_repo_indexes_expected_files
    assert result.files_indexed == 10


# ---------------------------------------------------------------- ignore rules (no DB)


def test_ignore_node_modules_excluded():
    assert not should_index(FIXTURE_REPO / "node_modules" / "junk.ts", FIXTURE_REPO)


def test_ignore_pycache_excluded():
    assert not should_index(FIXTURE_REPO / "__pycache__" / "junk.py", FIXTURE_REPO)


def test_ignore_venv_excluded(tmp_path):
    f = tmp_path / ".venv" / "lib" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    assert not should_index(f, tmp_path)


def test_ignore_dts_excluded(tmp_path):
    f = tmp_path / "types.d.ts"
    f.write_text("export {}\n")
    assert not should_index(f, tmp_path)


def test_ignore_oversized_excluded(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"x{i} = {i}" for i in range(5001)))
    assert not should_index(f, tmp_path)


def test_ignore_test_tsx_included():
    f = FIXTURE_REPO / "src" / "components" / "__tests__" / "dashboard.test.tsx"
    assert should_index(f, FIXTURE_REPO)


def test_ignore_test_py_included():
    f = FIXTURE_REPO / "tests" / "test_jwt.py"
    assert should_index(f, FIXTURE_REPO)


def test_ignore_prisma_included():
    # sanctioned task-02 expectation flip (03b-prisma.md #5): schema.prisma
    # is now a supported extension, so it's indexed rather than skipped.
    assert should_index(FIXTURE_REPO / "schema.prisma", FIXTURE_REPO)
