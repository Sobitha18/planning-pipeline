# Task: Build the Symbol Extractor (foundation of the code indexer)

## Context

We are building an SDLC planning pipeline. Its foundation is a code indexer, and the atom of the indexer is the **symbol extractor**: a pure function that takes one source file and returns its symbols. No database, no git, no embeddings — just parse and extract. Everything else stacks on this output.

Target codebase is Next.js/React/TypeScript (with Prisma, Zod, next-safe-action, cva/Tailwind), so TS/TSX support is primary. Python support is also required.

## Dependencies

```
pip install tree-sitter tree-sitter-python tree-sitter-typescript pytest
```
Use tree-sitter >= 0.26 (API: `tree_sitter.Query`, `tree_sitter.QueryCursor(query).captures(node)` returning `{capture_name: [nodes]}`).

## Files to create

```
src/indexer/extractor.py           # the extractor
src/indexer/queries/python.scm     # tree-sitter capture patterns, Python
src/indexer/queries/tsx.scm        # tree-sitter capture patterns, TS/TSX
tests/test_extractor.py            # 13 tests, both languages
tests/fixtures/sample_repo/src/auth/jwt.py            # Python fixture
tests/fixtures/sample_repo/src/components/dashboard.tsx  # TSX fixture
```
Add `__init__.py` files as needed for imports.

## Public API

```python
@dataclass(frozen=True)
class Symbol:
    kind: str             # class|function|method|interface|type|enum|const
    name: str
    qualified_name: str   # e.g. "components.ui.Button", "auth.jwt.JwtValidator.verify"
    start_line: int       # 1-based inclusive; span includes decorators/export keywords
    end_line: int
    signature: str        # header line(s) verbatim, decorators/export included, body excluded
    docstring: str | None # Python docstring / TS leading JSDoc, quote/comment markers stripped
    exported: bool        # TS: inside export statement; Python: name doesn't start with "_"
    language: str
    def to_dict(self) -> dict: ...

def extract(file_path: str, content: str | bytes,
            repo_root: str | None = None) -> list[Symbol]: ...

SUPPORTED_EXTENSIONS: frozenset  # .py .ts .tsx .js .jsx .mjs .cjs
```

## Behavior requirements

1. **Language registry keyed by file extension.** One TSX grammar (`tree_sitter_typescript.language_tsx()`) handles .ts/.tsx/.js/.jsx/.mjs/.cjs. Adding a language later = grammar + .scm + one registry entry.
2. **Unsupported extensions return `[]`** (e.g. .css, .prisma). Never raise.
3. **Broken syntax never raises** — tree-sitter yields a partial tree; extract whatever parsed. Zero symbols is a valid outcome.
4. **Qualified names** = module path + enclosing scopes + name.
   - Module path from file path relative to `repo_root`: drop extension, drop a leading `src/` or `lib/` segment, drop trailing `__init__` (py) or `index` (ts). `src/components/ui/button.tsx` → `components.ui.button`; `app/dashboard/page.tsx` → `app.dashboard.page`.
   - Nesting: `auth.jwt.JwtValidator.verify`, `components.dashboard.Button.handleClick`.
5. **Kinds:**
   - Python: class / function / method (function whose nearest enclosing def is a class). Function nested inside a function = function, not method.
   - TS: class / method (method_definition) / interface / type (type_alias) / enum / function / const.
6. **TS declarator patterns (critical for this codebase):**
   - `const Foo = () => ...` and `const foo = function() {}` → kind **function** (name from the variable_declarator — arrow functions have no name of their own). This is the standard React component pattern.
   - `const x = someCall(...)` (e.g. `z.object({...})`, `cva(...)`) → kind **const**. Zod schemas and cva variants must be captured — validation logic lives there.
   - A declarator matching both rules resolves to **function** (more specific wins).
7. **Spans widen upward:** through `decorated_definition` (py), and `variable_declarator` → `lexical_declaration`/`variable_declaration` → `export_statement` (ts). So a decorated method's span starts at the decorator; an exported const component's span starts at `export`.
8. **Signature** = text from span start to body start; strip trailing `{`; Python signatures end with `:`; cap at 500 chars. Multi-line signatures (async fn with multiline params) captured whole.
9. **Docstrings:** Python — first statement of body if plain string, prefix+quotes stripped. TS — JSDoc `/** ... */` comment immediately preceding the widened span, `*` gutters stripped, joined with newlines.
10. **Exported flag (TS):** walk up looking for `export_statement`, but STOP at any enclosing function/class scope — a helper nested inside an exported component is NOT itself exported. Python: `not name.startswith("_")`.

## .scm capture patterns

Name captures `<kind>.def` (whole node) and `<kind>.name`. Keep queries minimal; handle decorators/export-widening in Python code, not in the query.

python.scm: `class_definition`, `function_definition`.

tsx.scm: `function_declaration`, `generator_function_declaration`, `class_declaration` (name is type_identifier), `method_definition` (name is property_identifier), `interface_declaration`, `type_alias_declaration`, `enum_declaration`, plus the two variable_declarator patterns from requirement 6.

## Fixtures (must contain these exact cases)

**jwt.py** — module docstring; a class with: docstring, class attr, a method with docstring, a method decorated with `@functools.lru_cache(maxsize=128)` and no docstring, a nested inner class with a method; a module-level function decorated with `@functools.wraps(print)` that is `async` with a multi-line signature and contains a nested function; a private `_module_private()` function.

**dashboard.tsx** — an interface with JSDoc; a type alias; `export const userSchema = z.object({...})`; `export const buttonVariants = cva(...)`; an exported arrow-function component with JSDoc containing a nested `handleClick` arrow; `export async function createUser(...)` (server action shape); `export default function DashboardPage()`; a non-exported class with an async method; an enum. Imports of zod/cva/prisma at top.

## Tests (13 total; parse fixtures with repo_root=sample_repo)

Python (6):
1. Exactly these qualified names found: `auth.jwt.JwtValidator`, `.verify`, `._decode`, `.Inner`, `.Inner.ping`, `auth.jwt.refresh_token`, `auth.jwt.refresh_token._helper`, `auth.jwt._module_private`
2. Kinds: JwtValidator=class, verify=method, Inner=class, refresh_token=function, `_helper`=function (nested-in-function is NOT a method)
3. Decorated spans: `_decode.signature` starts with `@functools.lru_cache`; refresh_token signature contains the decorator, contains `async def refresh_token(`, ends with `-> str:`
4. Docstrings: verify == "Validates JWT and returns claims."; `_decode` is None
5. All spans satisfy 1 <= start <= end
6. `extract("x/y.py", "def ok():\n    pass\n\ndef broken(:\n")` does not raise and still finds `ok`

TypeScript (7):
1. All expected qualified names present (superset ok): ButtonProps, UserId, userSchema, buttonVariants, Button, Button.handleClick, createUser, DashboardPage, ApiClient, ApiClient.get, Role — all prefixed `components.dashboard.`
2. Kinds: ButtonProps=interface, UserId=type, userSchema=const, Button=function, ApiClient=class, ApiClient.get=method, Role=enum
3. Exported: Button/createUser/DashboardPage true; ApiClient/ButtonProps false; **Button.handleClick false** (nested helper)
4. JSDoc: Button docstring contains "Renders a styled button."; ButtonProps docstring == "Props for the button component."
5. Signatures: createUser == `export async function createUser(input: z.infer<typeof userSchema>)`; Button starts with `export const Button = (`
6. `extract("styles/globals.css", "body{}") == []`
7. Broken TS (`export function ok() { return 1 }\nconst broken = ((( \n`) does not raise, still finds `ok`

## Definition of done

`python3 -m pytest tests/ -q` → 13 passed. No network calls, no DB, pure function.

## Out of scope (do NOT build now)

Edge/call-graph resolution, embeddings/chunking, repo walking, incremental updates, Prisma schema parsing, Next.js route metadata, any storage.
