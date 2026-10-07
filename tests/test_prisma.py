"""Prisma parser tests: DB-free unit tests on parse_prisma() plus a
DB-backed end-to-end check through the full index/query stack."""

from pathlib import Path

from src.indexer.prisma import parse_prisma

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
SCHEMA_PATH = FIXTURE_REPO / "prisma" / "schema.prisma"
SCHEMA_REL = "prisma/schema.prisma"
CONTENT = SCHEMA_PATH.read_text()


def _symbols():
    return parse_prisma(SCHEMA_REL, CONTENT, repo_root=str(FIXTURE_REPO))


# ---------------------------------------------------------------- DB-free


def test_models_and_enum_extracted_generator_datasource_absent():
    syms = _symbols()
    by_name = {s.name: s for s in syms}

    assert by_name["User"].kind == "model"
    assert by_name["Post"].kind == "model"
    assert by_name["Role"].kind == "enum"
    assert "db" not in by_name and "client" not in by_name  # datasource/generator skipped

    user = by_name["User"]
    assert user.qualified_name == "prisma.schema.User"
    assert user.start_line == 12  # the `model User {` line itself
    assert CONTENT.splitlines()[user.start_line - 1].strip() == "model User {"
    assert CONTENT.splitlines()[user.end_line - 1].strip() == "}"


def test_doc_comment_and_signature():
    syms = _symbols()
    user = next(s for s in syms if s.name == "User")

    assert user.docstring == (
        "A registered platform user.\nOwns zero or more posts."
    )
    assert "email     String   @unique" in user.signature
    assert "@@index([email])" in user.signature
    assert user.exported is True
    assert user.language == "prisma"


def test_malformed_trailing_block_does_not_raise():
    syms = _symbols()
    names = {s.name for s in syms}
    assert names == {"User", "Post", "Role"}
    assert "Broken" not in names


def test_broken_schema_partial_parse_never_raises():
    partial = "model Ok {\n  id String\n}\n\nmodel Trailing {\n  id String\n"
    syms = parse_prisma("x.prisma", partial, repo_root=None)
    assert [s.name for s in syms] == ["Ok"]


# ---------------------------------------------------------------- DB-backed


def test_end_to_end_index_and_query(db_url):
    from src.indexer.full_index import index_repo
    from src import index_query as iq

    result = index_repo(str(FIXTURE_REPO))
    assert result.errors == []

    repo_id = iq.resolve_repo_id(str(FIXTURE_REPO))
    assert repo_id is not None

    hits = iq.find_symbols(repo_id, "User")
    assert any(
        h.qualified_name == "prisma.schema.User" and h.path == SCHEMA_REL
        for h in hits
    )

    text_hits = iq.search_text(repo_id, "authorId", mode="trigram")
    assert any(h.path == SCHEMA_REL for h in text_hits)
