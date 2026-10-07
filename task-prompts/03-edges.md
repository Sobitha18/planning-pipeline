# Task 03: Symbol Edges (import/call resolution)

## Context

Steps 1-2 (done): extractor (`src/indexer/extractor.py`) and repo indexer + query API (`src/indexer/full_index.py`, `src/index_query.py`) storing files/symbols in Postgres. Do NOT modify the extractor's public API.

This task: derive edges between symbols (imports, calls, implements/inherits) so Stage A can do 1-hop graph expansion and the plan validator can augment task dependencies. Must work for BOTH TypeScript/TSX and Python repos with zero language branching outside clearly marked per-language resolution modules.

## Files to create

```
src/indexer/edges.py            # orchestration + shared resolution logic
src/indexer/edges_python.py     # python import/call extraction (tree-sitter)
src/indexer/edges_ts.py         # ts/tsx import/call extraction (tree-sitter)
tests/test_edges.py
```

## Schema addition (add to db.py init)

```sql
symbol_edges(
  id           serial PK,
  repo_id      int FK -> repos ON DELETE CASCADE,
  from_symbol  int FK -> symbols ON DELETE CASCADE,
  to_symbol    int FK -> symbols ON DELETE CASCADE,
  kind         text NOT NULL,       -- imports | calls | implements | inherits
  confidence   text NOT NULL        -- resolved | heuristic
)
-- index on (repo_id, from_symbol), (repo_id, to_symbol); UNIQUE(from_symbol,to_symbol,kind)
```

Also add file-level import capture:
```sql
file_imports(
  file_id      int FK -> files ON DELETE CASCADE,
  imported_path text NOT NULL,     -- resolved repo-relative file path, or NULL if external
  raw_specifier text NOT NULL,     -- "./utils", "@/lib/prisma", "zod", "functools"
  names         text[] NOT NULL    -- imported names; ["*"] for namespace/star
)
```

## Resolution logic — tiered confidence, never fake precision

Two-pass, run as `build_edges(repo_id)` after full indexing (and callable independently):

**Pass 1 — collect:** for each file, parse (tree-sitter, reuse grammars from extractor) and extract:
- import statements → file_imports rows
- per-symbol: call expressions (callee names), base classes / implemented interfaces, attribute calls (`obj.method(...)` → record method name)

**Pass 2 — resolve against the full symbol table:**
- **Import specifier → file path resolution:**
  - TS: relative (`./x`, `../x`) resolved against importing file dir; alias `@/` → `src/` (read `tsconfig.json` `paths` if present, else default `@/*`→`src/*`); try extensions `.ts .tsx .js .jsx` and `/index.*`. Bare specifiers (`zod`, `react`) = external → imported_path NULL.
  - Python: absolute module dotted path → file path (`a.b.c` → `a/b/c.py` or `a/b/c/__init__.py`, also try under `src/`); relative imports (`from . import x`) resolved from importing file's package.
- **Edge kinds:**
  - `imports` (confidence=resolved): symbol A's file imports name N from file F, and F defines symbol N → edge file-A-symbols-using-N → N. If usage tracking is too fine, emit one edge per (importing symbol that references N) using simple name-occurrence check within the symbol's line span.
  - `calls`: callee name resolution order → (1) same-file symbol (resolved), (2) imported name traced to its defining symbol (resolved), (3) name match within same top-level package, ≤5 candidates → edge to each (heuristic), (4) >5 candidates repo-wide → drop.
  - `inherits`/`implements` (TS: `extends`/`implements` clauses; Py: base classes): resolve like calls; resolved-or-heuristic same rules.
- Method calls on unknown receivers (`this.x()`, `self.x()`): resolve within the enclosing class first (resolved), else drop.

## Query API additions (src/index_query.py)

```python
def neighbors(repo_id, symbol_ids: list[int], *,
              direction: str = "both",        # callers|callees|both
              kinds: list[str] | None = None,
              min_confidence: str = "heuristic",   # "resolved" filters stricter
              limit: int = 50) -> list[SymbolHit]

def imports_of_file(repo_id, path: str) -> list[dict]   # file_imports rows
def importers_of_file(repo_id, path: str) -> list[str]  # reverse lookup
```

## Tests (tests/test_edges.py)

Extend `tests/fixtures/sample_repo/` with files that exercise resolution:
- TS: `src/lib/prisma.ts` exporting `prisma`; make `dashboard.tsx`'s existing `@/lib/prisma` import resolve to it; a `src/components/list.tsx` importing `{ Button }` from `"./dashboard"` and calling it; a class `extends` case.
- Python: `src/auth/service.py` doing `from auth.jwt import JwtValidator`, instantiating and calling `.verify()`; a subclass of JwtValidator.

Assertions:
1. TS relative import resolves: list.tsx → dashboard.tsx edge (imports, resolved), Button call edge (calls, resolved).
2. TS alias `@/lib/prisma` resolves to src/lib/prisma.ts; bare `zod` import → file_imports row with imported_path NULL, no symbol edge.
3. Python `from auth.jwt import JwtValidator` → imports edge resolved; `validator.verify()` inside same class context → calls edge to JwtValidator.verify (resolved via import trace + class member).
4. Inherits edge present and resolved in both languages.
5. Ambiguous bare-name call (add two same-named helpers in different modules, call by bare name from a third) → heuristic edges ≤5 or dropped; NEVER marked resolved.
6. `neighbors(direction="callers")` on Button returns list.tsx's caller; `min_confidence="resolved"` excludes heuristic edges.
7. Idempotency: build_edges twice → same edge count (unique constraint respected via upsert/ignore).
8. Unresolvable import (`./does-not-exist`) → no crash, no edge, recorded in returned stats.

DB tests skip cleanly without TEST_DATABASE_URL (same pattern as step 2).

## Real-repo smoke (definition of done, run and report results)

Index + build_edges on BOTH:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Report: total edges by kind and confidence; spot-check that `neighbors()` on one well-known component/function in each repo returns plausible callers/callees; zero crashes; % of imports resolved (expect >70% internal imports resolved — report the number, don't fail on it).

## Out of scope

Type-inference-based resolution, tsserver/pyright integration, dynamic import handling, re-export/barrel chain following beyond one level, embeddings, incremental edge updates (full rebuild per run is fine).
