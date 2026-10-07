"""The atom of the indexer: extract(path, content) -> list[Symbol].

One source file in, its symbols out. No DB, no git, no embeddings.
Language support is a registry keyed by file extension; adding a language
means: install its tree-sitter grammar, write a queries/<lang>.scm, and
register it in _LANGUAGES below.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path

import tree_sitter
import tree_sitter_python
import tree_sitter_typescript

_QUERIES_DIR = Path(__file__).parent / "queries"

# ---------------------------------------------------------------- data model


@dataclass(frozen=True)
class Symbol:
    kind: str              # class|function|method|interface|type|enum|const|model
    name: str               # "verify"
    qualified_name: str     # "components.ui.Button" / "auth.jwt.JwtValidator.verify"
    start_line: int         # 1-based, inclusive; includes decorators / export kw
    end_line: int           # 1-based, inclusive
    signature: str          # header line(s) verbatim
    docstring: str | None   # Python docstring / TS leading JSDoc
    exported: bool = False  # TS: inside an export statement (or Python: public name)
    language: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- language registry


@dataclass
class _Lang:
    name: str
    language: tree_sitter.Language
    query: tree_sitter.Query
    parser: tree_sitter.Parser = field(init=False)

    def __post_init__(self) -> None:
        self.parser = tree_sitter.Parser(self.language)


def _make_lang(name: str, ts_language, query_file: str) -> _Lang:
    lang = tree_sitter.Language(ts_language)
    query = tree_sitter.Query(lang, (_QUERIES_DIR / query_file).read_text())
    return _Lang(name=name, language=lang, query=query)


_PY = _make_lang("python", tree_sitter_python.language(), "python.scm")
_TSX = _make_lang("typescript", tree_sitter_typescript.language_tsx(), "tsx.scm")
# language_tsx() parses plain TS and JS fine as well; one grammar covers all four.

_LANGUAGES: dict[str, _Lang] = {
    ".py": _PY,
    ".ts": _TSX, ".tsx": _TSX, ".js": _TSX, ".jsx": _TSX,
    ".mjs": _TSX, ".cjs": _TSX,
}

SUPPORTED_EXTENSIONS = frozenset(_LANGUAGES) | {".prisma"}

# node types that open a new naming scope, per family
_PY_SCOPES = {"class_definition", "function_definition"}
_TS_SCOPES = {"class_declaration", "function_declaration",
              "generator_function_declaration", "method_definition",
              "arrow_function", "function_expression"}


# ---------------------------------------------------------------- helpers


def _module_path(file_path: str, repo_root: str | None = None) -> str:
    """src/components/ui/button.tsx -> components.ui.button
    app/dashboard/page.tsx          -> app.dashboard.page
    src/auth/jwt.py                 -> auth.jwt
    """
    p = Path(file_path)
    if repo_root:
        try:
            p = p.relative_to(repo_root)
        except ValueError:
            pass
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] in ("__init__", "index"):
        parts = parts[:-1]
    return ".".join(parts)


def _node_name(node: tree_sitter.Node) -> str | None:
    n = node.child_by_field_name("name")
    return n.text.decode() if n is not None else None


def _enclosing_names(node: tree_sitter.Node, scope_types: set[str]) -> list[str]:
    """Names of enclosing named scopes, outermost first.

    For TS, an arrow function itself has no name — its name lives on the
    variable_declarator parent, which we pick up when crossing it.
    """
    names: list[str] = []
    cur = node.parent
    while cur is not None:
        if cur.type in scope_types:
            name = _node_name(cur)
            if name is None and cur.parent is not None and \
                    cur.parent.type == "variable_declarator":
                name = _node_name(cur.parent)
            if name:
                names.append(name)
        cur = cur.parent
    return list(reversed(names))


def _span_node(node: tree_sitter.Node) -> tree_sitter.Node:
    """Widen the span to decorators / export statements / const declaration."""
    cur = node
    while cur.parent is not None and cur.parent.type in (
        "decorated_definition",        # python decorators
        "variable_declarator",         # ts: up to the declarator
        "lexical_declaration",         # ts: const/let line
        "variable_declaration",        # js: var line
        "export_statement",            # ts: export wrapper
    ):
        cur = cur.parent
    return cur


def _is_exported(node: tree_sitter.Node) -> bool:
    """True if wrapped in an export statement — without crossing into an
    enclosing function/class (a nested helper is not itself exported)."""
    cur = node.parent
    while cur is not None:
        if cur.type == "export_statement":
            return True
        if cur.type in _TS_SCOPES:
            return False
        cur = cur.parent
    return False


def _signature(node: tree_sitter.Node, span: tree_sitter.Node,
                source: bytes) -> str:
    """Text from span start to the body start (or whole node if no body)."""
    body = node.child_by_field_name("body")
    header_end = body.start_byte if body is not None else node.end_byte
    header_end = max(header_end, span.start_byte)
    text = source[span.start_byte:header_end].decode(errors="replace").rstrip()
    if text.endswith("{"):
        text = text[:-1].rstrip()
    if node.type in ("class_definition", "function_definition"):  # python
        text = text.rstrip(":") + ":"
    # cap for pathological cases
    return text if len(text) <= 500 else text[:500] + "…"


def _py_docstring(node: tree_sitter.Node) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return None
    first = body.named_child(0)
    if first.type != "expression_statement" or first.named_child_count == 0:
        return None
    expr = first.named_child(0)
    if expr.type != "string":
        return None
    raw = expr.text.decode(errors="replace")
    i = 0
    while i < len(raw) and raw[i] not in "\"'":
        i += 1
    raw = raw[i:]
    for q in ('"""', "'''", '"', "'"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
            return raw[len(q):-len(q)].strip()
    return raw


def _ts_jsdoc(span: tree_sitter.Node) -> str | None:
    """JSDoc block comment immediately preceding the (widened) definition.

    Excluded: a comment starting on line 1 (a file banner, not a symbol's
    own doc) and a comment separated from the span by a blank line.
    """
    prev = span.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    if prev.start_point[0] == 0:
        return None
    if span.start_point[0] - prev.end_point[0] > 1:
        return None
    text = prev.text.decode(errors="replace")
    if not text.startswith("/**"):
        return None
    lines = [ln.strip().lstrip("*").strip()
             for ln in text[3:].rstrip("*/").splitlines()]
    return "\n".join(ln for ln in lines if ln) or None


def _is_nested(node: tree_sitter.Node, scope_types: set[str]) -> bool:
    """True if node sits inside any enclosing function/method/arrow scope."""
    cur = node.parent
    while cur is not None:
        if cur.type in scope_types:
            return True
        cur = cur.parent
    return False


def _in_class(node: tree_sitter.Node, is_py: bool) -> bool:
    class_t = "class_definition" if is_py else "class_declaration"
    scopes = _PY_SCOPES if is_py else _TS_SCOPES
    cur = node.parent
    while cur is not None:
        if cur.type == class_t:
            return True
        if cur.type in scopes and cur.type != class_t:
            return False
        cur = cur.parent
    return False


# ---------------------------------------------------------------- public API


def extract(file_path: str, content: str | bytes,
            repo_root: str | None = None) -> list[Symbol]:
    """Parse one source file and return its symbols.

    Unsupported extensions return []. Bad syntax never raises: tree-sitter
    yields a partial tree and we extract whatever parsed.
    """
    ext = Path(file_path).suffix
    if ext == ".prisma":
        from src.indexer.prisma import parse_prisma  # local: avoid import cycle
        return parse_prisma(file_path, content, repo_root)

    lang = _LANGUAGES.get(ext)
    if lang is None:
        return []

    source = content.encode() if isinstance(content, str) else content
    tree = lang.parser.parse(source)
    module = _module_path(file_path, repo_root)
    is_py = lang.name == "python"
    scope_types = _PY_SCOPES if is_py else _TS_SCOPES

    captures = tree_sitter.QueryCursor(lang.query).captures(tree.root_node)

    # (kind, node) pairs, deduped by node, in source order
    seen: dict[int, tuple[str, tree_sitter.Node]] = {}
    for cap_name, nodes in captures.items():
        if not cap_name.endswith(".def"):
            continue
        kind = cap_name.split(".")[0]
        for n in nodes:
            # a declarator matching both function.def and const.def:
            # prefer the more specific "function" kind
            if id(n) in seen and kind == "const":
                continue
            seen[id(n)] = (kind, n)
    ordered = sorted(seen.values(), key=lambda kn: kn[1].start_byte)

    symbols: list[Symbol] = []
    for kind, node in ordered:
        name = _node_name(node)
        if name is None:
            continue
        # const x = someCall(...) is only a symbol at module scope; a local
        # inside a function/method/arrow body is just a local variable.
        if kind == "const" and _is_nested(node, scope_types):
            continue
        scope = _enclosing_names(node, scope_types)
        # drop own name if the declarator itself was picked up as a scope
        if scope and scope[-1] == name:
            scope = scope[:-1]

        if kind == "function" and _in_class(node, is_py):
            kind = "method"

        span = _span_node(node)
        qualified = ".".join(x for x in (module, *scope, name) if x)

        if is_py:
            doc = _py_docstring(node)
            exported = not name.startswith("_")
            sig_node = node
        else:
            value = node.child_by_field_name("value")
            sig_node = value if node.type == "variable_declarator" and \
                value is not None else node
            doc = _ts_jsdoc(span)
            exported = _is_exported(node)

        symbols.append(Symbol(
            kind=kind, name=name, qualified_name=qualified,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            signature=_signature(sig_node, span, source),
            docstring=doc, exported=exported, language=lang.name,
        ))
    return symbols
