# Task: Build the Repo Indexer + Query API (step 2, on top of the extractor)

## Context

Step 1 (done): `src/indexer/extractor.py` — `extract(file_path, content, repo_root) -> list[Symbol]`, multi-language (TS/TSX primary, Python), validated on the real repo. Do NOT modify it in this task.

Step 2 (this task): walk a repo, run the extractor on every supported file, store results in Postgres, and expose a small internal query API. This turns the extractor into an actual index that the planning pipeline's Stage A (context retrieval) will consume later.

Target repos are BOTH kinds: Next.js/TypeScript (Prisma, Zod, App Router) AND Python projects (FastAPI/Django-style layouts). Everything in this task must be language-neutral — the extractor already handles both; nothing in the walk/store/query layer may assume one language.

## Dependencies

```
pip install sqlalchemy psycopg[binary] pytest
```
Postgres is assumed running; connection string from env `DATABASE_URL` (e.g. `postgresql+psycopg://user:pass@localhost:5432/ppl`). Use SQLAlchemy Core or ORM (your choice), but keep it simple. No alembic yet — a `create_all()`-style schema init function is fine for this step.

## Files to create

```
src/config.py            # settings from env (DATABASE_URL), simple dataclass/pydantic
src/db.py                # engine, session factory, init_schema()
src/indexer/ignore.py    # file filtering rules
src/indexer/full_index.py# walk repo -> extract -> store
src/index_query.py       # internal query API over the stored index
tests/test_full_index.py # tests (see below)
tests/test_index_query.py
```

## Database schema

```sql
repos(
  id            serial PK,
  root_path     text UNIQUE NOT NULL,     -- absolute path for now (local repos)
  indexed_sha   text NULL,                -- git HEAD sha at index time; NULL if not a git repo
  indexed_at    timestamptz NULL,
  status        text NOT NULL DEFAULT 'empty'  -- empty|building|ready|failed
)

files(
  id            serial PK,
  repo_id       int FK -> repos ON DELETE CASCADE,
  path          text NOT NULL,            -- relative to repo root, posix-style
  language      text NOT NULL,
  content_hash  text NOT NULL,            -- sha256 of content
  loc           int NOT NULL,
  content       text NOT NULL,            -- full source, used for FTS + later context packing
  UNIQUE (repo_id, path)
)

symbols(
  id            serial PK,
  file_id       int FK -> files ON DELETE CASCADE,
  kind          text NOT NULL,
  name          text NOT NULL,
  qualified_name text NOT NULL,
  start_line    int NOT NULL,
  end_line      int NOT NULL,
  signature     text NOT NULL,
  docstring     text NULL,
  exported      bool NOT NULL
)
-- indexes: symbols(name), symbols(qualified_name), files(repo_id)

-- Full-text search: generated tsvector column on files.content
ALTER TABLE files ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;
CREATE INDEX files_content_tsv_idx ON files USING gin(content_tsv);
-- plus trigram for exact-ish string search:
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX files_content_trgm_idx ON files USING gin(content gin_trgm_ops);
```

Notes:
- Use `'simple'` FTS config, not `'english'` — code identifiers must not be stemmed.
- No embeddings/pgvector in this step (explicitly out of scope).

## src/indexer/ignore.py

```python
def should_index(path: Path, repo_root: Path) -> bool
```
Rules (in order):
1. Must have suffix in `SUPPORTED_EXTENSIONS` (import from extractor).
2. Skip if any path part in: `node_modules`, `.next`, `.git`, `dist`, `build`, `.turbo`, `coverage`, `.vercel`, `out`, `__pycache__`, `.venv`, `venv`, `env`, `.tox`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `site-packages`, `.eggs`, `migrations` (Django/alembic autogen — configurable, default skip).
3. Skip filename suffixes: `.d.ts`, `.min.js`. Skip exact names: `conftest.py` is INDEXED (it's real code), but `setup.py`/`versioneer.py` boilerplate is indexed too — no special-casing beyond the rules above.
4. Skip files > 1 MB or > 5000 lines (generated-code guard) — return False, caller logs.
5. If a `.gitignore` exists at repo root, respect its top-level patterns (simple implementation: use `pathspec` library — add to deps — with the gitignore syntax; do not hand-roll glob matching).
Test files ARE indexed in both ecosystems — `.test.ts`/`.spec.ts`/`__tests__/` and `test_*.py`/`*_test.py`/`tests/`/`conftest.py` (they encode expected behavior; the design needs them for `tests_of`).

## src/indexer/full_index.py

```python
def index_repo(root_path: str) -> IndexResult
```
Behavior:
1. Resolve root, upsert `repos` row, set status `building`.
2. Read git HEAD sha if `.git` exists (`git rev-parse HEAD` via subprocess; tolerate absence -> None).
3. Walk files (rglob), filter via `should_index`, sort for determinism.
4. For each file: read with `errors="replace"`, compute sha256, run `extract`. Extractor exceptions must be caught per-file: log, count as error, continue — one bad file never fails the run.
5. **Idempotent re-index:** if a `files` row exists with same (repo_id, path) and same content_hash, skip re-extraction (symbols already correct). If hash differs, delete old row (cascade removes symbols) and insert fresh. Files present in DB but no longer on disk (or now ignored) are deleted.
6. Batch inserts (e.g. per 200 files per transaction) — a 2k-file repo must not be 2k transactions.
7. On success set repos.status `ready`, indexed_sha, indexed_at. On unexpected fatal error set `failed` and re-raise.
8. Return `IndexResult` dataclass: `files_indexed, files_skipped_unchanged, files_deleted, symbols_count, errors: list[tuple[path, str]], duration_s`.

## src/index_query.py

All functions take `repo_id` (or repo root path resolved to id). Return plain dataclasses, not ORM rows.

```python
@dataclass
class SymbolHit:  # symbol fields + file path
    ...

@dataclass
class FileHit:
    path: str; score: float; snippet: str  # snippet = matched region ~3 lines

def find_symbols(repo_id, name: str, *, fuzzy: bool = False,
                 kind: str | None = None, exported_only: bool = False,
                 limit: int = 20) -> list[SymbolHit]
    # exact: name == or qualified_name endswith; fuzzy: ILIKE %name% on both

def search_text(repo_id, query: str, *, mode: str = "fts",  # "fts" | "trigram"
                limit: int = 10) -> list[FileHit]
    # fts: websearch_to_tsquery('simple', query), rank with ts_rank
    # trigram: content ILIKE %query% ordered by similarity(); for exact error strings

def get_file(repo_id, path: str) -> str | None          # full content

def get_symbols_in_file(repo_id, path: str) -> list[SymbolHit]

def tests_of(repo_id, symbol_names: list[str], limit: int = 10) -> list[FileHit]
    # heuristic: files whose path matches test conventions, BOTH ecosystems:
    #   TS/JS:  *.test.*, *.spec.*, __tests__/ dir
    #   Python: test_*.py, *_test.py, tests/ dir, conftest.py
    # AND whose content contains any of the given names (trigram/ILIKE)
```

No SQL outside db.py/index_query.py/full_index.py. Stage A will import ONLY from index_query.

## Tests

Use the existing `tests/fixtures/sample_repo/` (has src/auth/jwt.py and src/components/dashboard.tsx — already dual-language). Add to it: a `node_modules/junk.ts` and a `__pycache__/junk.pyc`-style dir and a `schema.prisma` (all must be ignored), a `src/components/__tests__/dashboard.test.tsx` containing the string `Button`, and a `tests/test_jwt.py` inside the fixture repo containing the string `JwtValidator` (for Python tests_of).

Postgres for tests: read `TEST_DATABASE_URL` env; if unset, skip DB tests with a clear pytest skip message (so the suite still runs without a DB). Use a fresh schema per test session (drop/create tables in a fixture).

test_full_index.py:
1. Indexing sample_repo succeeds: status ready, files_indexed == exactly the 2 source files + 2 test files (jwt.py, dashboard.tsx, dashboard.test.tsx, test_jwt.py); node_modules, __pycache__, and schema.prisma excluded; symbols_count > 0 with symbols from BOTH languages present.
2. Symbols queryable: a known qualified_name from each fixture file exists in DB.
3. Idempotency: second run reports all files skipped_unchanged, zero re-extracted; symbol ids unchanged.
4. Change detection: modify one fixture file content (in a tmp copy of sample_repo), re-index → exactly 1 file re-indexed, its old symbols gone, new present.
5. Deletion: remove a file from the tmp copy, re-index → files_deleted == 1, its symbols gone.
6. Per-file error tolerance: monkeypatch extract to raise for one path → run completes, error recorded, other files indexed.
7. ignore rules unit tests (no DB needed): node_modules excluded, __pycache__/.venv excluded, .d.ts excluded, oversized file excluded, .test.tsx INCLUDED, test_*.py INCLUDED, schema.prisma excluded (unsupported extension).

test_index_query.py (against indexed sample_repo):
1. find_symbols exact: "Button" returns the component with correct path/kind.
2. find_symbols fuzzy: "valid" matches JwtValidator.
3. find_symbols kind filter: kind="interface" returns ButtonProps, not Button.
4. search_text fts: "Validates JWT" hits src/auth/jwt.py with a snippet containing the phrase.
5. search_text trigram: exact string "z.infer<typeof userSchema>" hits dashboard.tsx.
6. get_symbols_in_file returns all symbols of dashboard.tsx in line order.
7. tests_of(["Button"]) returns the __tests__ file; tests_of(["JwtValidator"]) returns tests/test_jwt.py (both conventions work).

## Definition of done

- `pytest tests/ -q` green (extractor's 13 + new ones; DB tests skip cleanly when TEST_DATABASE_URL unset).
- Manual smoke on BOTH real repos:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Each completes in seconds, errors empty; `find_symbols(repo_id, 'TagFilter')` returns the component in assure42; a known symbol resolves in clinical-ops too. Additionally index one Python repo (this planning-pipeline project itself works) — `find_symbols(repo_id, 'extract')` finds the extractor function. Same code path, zero language branching outside the extractor.

## Out of scope (do NOT build)

Embeddings/pgvector, edges/call-graph resolution, webhook/incremental git-diff updates (idempotent full re-run IS our update mechanism for now), arch_summary generation, Prisma schema parsing, any HTTP API, alembic migrations, async DB access.
