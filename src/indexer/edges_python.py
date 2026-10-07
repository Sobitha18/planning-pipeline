"""Python pass-1 extraction + import-path resolution for edges.py.

tree-sitter-python node shapes relied on here:
  import_statement          "import a.b.c [as x]"      -> field "name": dotted_name | aliased_import (repeats)
  import_from_statement     "from a.b import x[, y]"   -> field "module_name": dotted_name | relative_import
                                                           field "name": dotted_name | aliased_import (repeats)
                                                           or a bare "wildcard_import" child for `import *`
  call                      obj.method(...)            -> field "function": identifier | attribute
  class_definition          class Foo(Base, ...):      -> field "superclasses": argument_list
"""

from __future__ import annotations

import tree_sitter
import tree_sitter_python

from src.indexer.edges import FileParse, RawBase, RawCall, RawImport

_LANGUAGE = tree_sitter.Language(tree_sitter_python.language())
_PARSER = tree_sitter.Parser(_LANGUAGE)


def parse_file(content: str) -> FileParse:
    source = content.encode() if isinstance(content, str) else content
    tree = _PARSER.parse(source)
    parsed = FileParse()

    def walk(node: tree_sitter.Node) -> None:
        if node.type == "import_statement":
            _collect_plain_import(node, parsed.imports)
        elif node.type == "import_from_statement":
            _collect_from_import(node, parsed.imports)
        elif node.type == "call":
            _collect_call(node, parsed.calls)
        elif node.type == "class_definition":
            _collect_bases(node, parsed.bases)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return parsed


def _dotted_name(node: tree_sitter.Node) -> str:
    return node.text.decode(errors="replace")


def _collect_plain_import(node: tree_sitter.Node, out: list[RawImport]) -> None:
    line = node.start_point[0] + 1
    for i in range(node.child_count):
        if node.field_name_for_child(i) != "name":
            continue
        child = node.child(i)
        if child.type == "aliased_import":
            dotted = child.child_by_field_name("name")
            raw = _dotted_name(dotted) if dotted is not None else _dotted_name(child)
        else:
            raw = _dotted_name(child)
        # `import a.b.c` binds the whole namespace (accessed as a.b.c...);
        # store as a namespace import, matching the ["*"] convention.
        out.append(RawImport(raw_specifier=raw, names=["*"], line=line))


def _collect_from_import(node: tree_sitter.Node, out: list[RawImport]) -> None:
    module_node = node.child_by_field_name("module_name")
    if module_node is None:
        return
    line = node.start_point[0] + 1
    level = 0
    if module_node.type == "relative_import":
        raw_specifier = ""
        for c in module_node.children:
            if c.type == "import_prefix":
                level = c.text.decode().count(".")
            elif c.type == "dotted_name":
                raw_specifier = _dotted_name(c)
    else:
        raw_specifier = _dotted_name(module_node)

    names: list[str] = []
    is_star = False
    for i in range(node.child_count):
        child = node.child(i)
        if child.type == "wildcard_import":
            is_star = True
        elif node.field_name_for_child(i) == "name":
            if child.type == "aliased_import":
                nm = child.child_by_field_name("name")
                names.append(_dotted_name(nm) if nm is not None else _dotted_name(child))
            else:
                names.append(_dotted_name(child))
    if is_star:
        names = ["*"]
    if not names:
        return
    out.append(RawImport(
        raw_specifier=raw_specifier, names=names, line=line,
        is_relative=level > 0, level=level,
    ))


def _collect_call(node: tree_sitter.Node, out: list[RawCall]) -> None:
    fn = node.child_by_field_name("function")
    if fn is None:
        return
    line = node.start_point[0] + 1
    if fn.type == "identifier":
        out.append(RawCall(callee=fn.text.decode(), receiver=None, line=line))
    elif fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        if attr is None:
            return
        receiver = obj.text.decode(errors="replace") if obj is not None else None
        out.append(RawCall(callee=attr.text.decode(), receiver=receiver, line=line))


def _collect_bases(node: tree_sitter.Node, out: list[RawBase]) -> None:
    superclasses = node.child_by_field_name("superclasses")
    if superclasses is None:
        return
    class_line = node.start_point[0] + 1
    for child in superclasses.named_children:
        if child.type == "identifier":
            out.append(RawBase(class_line=class_line, base_name=child.text.decode()))
        elif child.type == "attribute":
            attr = child.child_by_field_name("attribute")
            if attr is not None:
                out.append(RawBase(class_line=class_line, base_name=attr.text.decode()))
        # keyword_argument (e.g. metaclass=...) is not a base class — skipped.


# ---------------------------------------------------------------- import-path resolution


def resolve_import(raw: RawImport, importing_path: str, known_paths: set[str]) -> str | None:
    """Dotted module path -> repo-relative file path, or None if it doesn't
    match any known file (stdlib/third-party, or a genuinely broken import)."""
    if raw.level > 0:
        parts = importing_path.split("/")[:-1]
        up = raw.level - 1
        if up:
            parts = parts[:-up] if up < len(parts) else []
        if raw.raw_specifier:
            # ponytail: `from . import x` (empty specifier) isn't resolved to
            # a file — could be a submodule or a name in __init__.py and we
            # don't disambiguate; add real resolution if that case matters.
            parts = parts + [p for p in raw.raw_specifier.split(".") if p]
        else:
            return None
        return _try_paths(parts, known_paths)

    mod_parts = [p for p in raw.raw_specifier.split(".") if p]
    for prefix in ([], ["src"]):
        found = _try_paths(prefix + mod_parts, known_paths)
        if found:
            return found
    return None


def _try_paths(parts: list[str], known_paths: set[str]) -> str | None:
    if not parts:
        return None
    base = "/".join(parts)
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if cand in known_paths:
            return cand
    return None
