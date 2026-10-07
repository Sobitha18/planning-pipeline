# Task 03b: Prisma Schema Indexing (mini-task)

## Context

The Next.js repos are Prisma-heavy — the data model lives in `prisma/schema.prisma`, currently ignored (unsupported extension). Data-related requests ("add a field to X", "why is Y not saving") need these models retrievable. This is a small task: parse Prisma models into symbols so they flow through the existing index/query/retrieval machinery unchanged. Run after task 02 (indexer); independent of 03 (edges) — can be done in the same session as 03 if capacity allows, otherwise separately.

## Files to create/modify

```
src/indexer/prisma.py        # parser
src/indexer/extractor.py     # register .prisma handling (see integration note)
src/indexer/ignore.py        # allow schema.prisma through
tests/test_prisma.py
```

## Parser (no tree-sitter grammar needed — regex/line-based is fine and sufficient)

`parse_prisma(file_path, content, repo_root) -> list[Symbol]` producing the SAME Symbol dataclass:
- Each `model X { ... }` → Symbol(kind="model", name=X, qualified_name=`prisma.X` (or module-path based — keep consistent: use `<module_path>.X`), signature = the full model block VERBATIM (fields, types, attributes — this is the valuable retrievable text; cap 500 chars like others), start/end lines, exported=True, language="prisma", docstring = `///` doc comments above the model if present)
- Each `enum X { ... }` → kind="enum", same treatment
- `generator`/`datasource` blocks: skipped
- Broken/partial schema: parse what matches, never raise (same guarantee as extractor)

Integration note: extend the extractor's registry pattern — either register a non-tree-sitter handler for `.prisma` in `_LANGUAGES`-adjacent dispatch, or have `extract()` delegate to `parse_prisma` for that suffix. Add "model" to the allowed kinds. `SUPPORTED_EXTENSIONS` must now include `.prisma` so the walker picks it up.

## Tests (tests/test_prisma.py)

Fixture: add `tests/fixtures/sample_repo/prisma/schema.prisma` with: a datasource block, a generator block, two models (one with `///` doc comment, relations between them, `@@index`), one enum, and a deliberately malformed trailing block.
1. Both models + enum extracted with correct kinds, names, line spans; generator/datasource absent.
2. Doc comment lands in docstring; model signature contains its field lines and `@@index`.
3. Malformed trailing block doesn't raise; prior models still extracted.
4. End-to-end: `index_repo(sample_repo)` now includes schema.prisma; `find_symbols(repo_id, "<ModelName>")` returns it; `search_text` trigram on a field name hits the file.
5. Update task-02's ignore test expectation: schema.prisma is now INCLUDED (fix the earlier test accordingly — this is the one sanctioned spec change).

## Real-repo smoke

Re-index BOTH repos:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
Report models/enums found per repo; `find_symbols` for 2 known model names returns them with full field signatures.

## Out of scope

Relation edges between models (could feed task 03 later — note as future), migrations parsing, Prisma client call-site linking.
