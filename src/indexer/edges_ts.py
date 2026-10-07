"""TS/TSX/JS/JSX pass-1 extraction + import-path resolution for edges.py.

Reuses the same tsx grammar as the extractor (covers ts/tsx/js/jsx alike).

tree-sitter-typescript node shapes relied on here:
  import_statement   field "import_clause": named_imports | namespace_import | identifier (default)
                      field "source": string
  call_expression    field "function": identifier | member_expression
  class_declaration  unfielded child "class_heritage" -> extends_clause (field "value")
                                                          + implements_clause (named_children: type_identifier)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import tree_sitter
import tree_sitter_typescript

from src.indexer.edges import FileParse, RawBase, RawCall, RawImport

_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_tsx())
_PARSER = tree_sitter.Parser(_LANGUAGE)

_EXTS = (".ts", ".tsx", ".js", ".jsx")
_DEFAULT_ALIAS = {"@/": "src/"}


def parse_file(content: str) -> FileParse:
    source = content.encode() if isinstance(content, str) else content
    tree = _PARSER.parse(source)
    parsed = FileParse()

    def walk(node: tree_sitter.Node) -> None:
        if node.type == "import_statement":
            _collect_import(node, parsed.imports)
        elif node.type == "call_expression":
            _collect_call(node, parsed.calls)
        elif node.type == "class_declaration":
            _collect_bases(node, parsed.bases)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return parsed


def _string_value(node: tree_sitter.Node) -> str:
    return node.text.decode(errors="replace").strip("'\"`")


def _collect_import(node: tree_sitter.Node, out: list[RawImport]) -> None:
    source = node.child_by_field_name("source")
    if source is None:
        return
    raw = _string_value(source)
    line = node.start_point[0] + 1
    # import_clause has no field name in this grammar — find it by type.
    clause = next((c for c in node.children if c.type == "import_clause"), None)
    names: list[str] = []
    if clause is not None:
        for child in clause.children:
            if child.type == "named_imports":
                for spec in child.named_children:
                    if spec.type != "import_specifier":
                        continue
                    nm = spec.child_by_field_name("name")
                    if nm is not None:
                        names.append(nm.text.decode())
            elif child.type == "namespace_import":
                names.append("*")
            elif child.type == "identifier":
                names.append("default")
    if not names:
        names = ["*"]  # side-effect import, e.g. `import "./polyfill"`
    out.append(RawImport(raw_specifier=raw, names=names, line=line))


def _collect_call(node: tree_sitter.Node, out: list[RawCall]) -> None:
    fn = node.child_by_field_name("function")
    if fn is None:
        return
    line = node.start_point[0] + 1
    if fn.type == "identifier":
        out.append(RawCall(callee=fn.text.decode(), receiver=None, line=line))
    elif fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None:
            return
        receiver = obj.text.decode(errors="replace") if obj is not None else None
        out.append(RawCall(callee=prop.text.decode(), receiver=receiver, line=line))


def _collect_bases(node: tree_sitter.Node, out: list[RawBase]) -> None:
    class_line = node.start_point[0] + 1
    for child in node.children:
        if child.type != "class_heritage":
            continue
        for hc in child.children:
            if hc.type == "extends_clause":
                val = hc.child_by_field_name("value")
                if val is not None:
                    out.append(RawBase(class_line=class_line, base_name=val.text.decode(), kind="inherits"))
            elif hc.type == "implements_clause":
                for t in hc.named_children:
                    if t.type in ("type_identifier", "identifier"):
                        out.append(RawBase(class_line=class_line, base_name=t.text.decode(), kind="implements"))


# ---------------------------------------------------------------- import-path resolution


def resolve_import(
    raw: RawImport, importing_path: str, known_paths: set[str], alias_map: dict[str, str],
) -> str | None:
    spec = raw.raw_specifier
    if spec.startswith("."):
        base = _normpath(str(Path(importing_path).parent / spec))
    else:
        base = None
        for prefix, target in alias_map.items():
            if spec.startswith(prefix):
                base = _normpath(target.rstrip("/") + "/" + spec[len(prefix):])
                break
        if base is None:
            return None  # bare specifier: external package (zod, react, ...)
    return _try_paths(base, known_paths)


def _normpath(p: str) -> str:
    return os.path.normpath(p).replace(os.sep, "/")


def _try_paths(base: str, known_paths: set[str]) -> str | None:
    if base in known_paths:
        return base
    for ext in _EXTS:
        if base + ext in known_paths:
            return base + ext
    for ext in _EXTS:
        cand = f"{base}/index{ext}"
        if cand in known_paths:
            return cand
    return None


# ---------------------------------------------------------------- tsconfig alias map


def load_alias_map(repo_root: Path) -> dict[str, str]:
    """{'@/': 'src/'} by default, or whatever tsconfig.json's compilerOptions.paths
    says (first target only; wildcard suffix stripped). Best-effort: any parse
    failure (missing file, non-JSON-parseable jsonc we can't strip) falls back
    to the default rather than failing the whole edges build."""
    tsconfig = repo_root / "tsconfig.json"
    if not tsconfig.exists():
        return dict(_DEFAULT_ALIAS)
    try:
        data = json.loads(_strip_json_comments(tsconfig.read_text(errors="replace")))
        paths = data.get("compilerOptions", {}).get("paths", {})
        alias = {}
        for key, targets in paths.items():
            if not targets:
                continue
            k = key[:-1] if key.endswith("*") else key
            v = targets[0][:-1] if targets[0].endswith("*") else targets[0]
            if k:
                alias[k] = v
        return alias or dict(_DEFAULT_ALIAS)
    except (OSError, ValueError, AttributeError, TypeError):
        return dict(_DEFAULT_ALIAS)


def _strip_json_comments(text: str) -> str:
    """Minimal jsonc -> json: drop // and /* */ comments outside of strings.
    tsconfig.json commonly has these; plain json.loads chokes on them."""
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)

