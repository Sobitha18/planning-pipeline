"""Prisma schema parser: regex/line-based, no tree-sitter grammar needed.

model/enum blocks are simple enough that brace-depth line scanning covers
schema.prisma fully. Reuses the extractor's Symbol shape and module-path
convention so prisma symbols flow through the index unchanged.
"""

from __future__ import annotations

import re

from src.indexer.extractor import Symbol, _module_path

_BLOCK_RE = re.compile(r"^\s*(model|enum|datasource|generator)\s+(\S+)\s*\{\s*$")
_DOC_RE = re.compile(r"^\s*///\s?(.*)$")

_EMIT_KINDS = {"model": "model", "enum": "enum"}  # datasource/generator: skipped


def _docstring(lines: list[str], header_idx: int) -> str | None:
    """`///` doc comment lines immediately above the block header (0-based)."""
    doc_lines: list[str] = []
    i = header_idx - 1
    while i >= 0:
        m = _DOC_RE.match(lines[i])
        if not m:
            break
        doc_lines.append(m.group(1))
        i -= 1
    return "\n".join(reversed(doc_lines)) if doc_lines else None


def parse_prisma(file_path: str, content: str | bytes,
                  repo_root: str | None = None) -> list[Symbol]:
    """Parse model/enum blocks out of a .prisma schema file.

    generator/datasource blocks are skipped. Never raises: an unterminated
    (malformed) trailing block is dropped, symbols found before it stand.
    """
    if isinstance(content, bytes):
        content = content.decode(errors="replace")
    lines = content.splitlines()
    module = _module_path(file_path, repo_root)

    symbols: list[Symbol] = []
    i = 0
    while i < len(lines):
        m = _BLOCK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        kind_kw, name = m.group(1), m.group(2)
        start = i
        depth = lines[i].count("{") - lines[i].count("}")
        j = i
        # ponytail: brace counting, not a real grammar — prisma blocks don't
        # nest braces inside attributes, so this is sufficient and simple.
        while depth > 0 and j + 1 < len(lines):
            j += 1
            depth += lines[j].count("{") - lines[j].count("}")

        if depth != 0:  # ran off EOF: malformed trailing block, stop here
            break

        if kind_kw in _EMIT_KINDS:
            block = "\n".join(lines[start:j + 1])
            qualified = f"{module}.{name}" if module else name
            symbols.append(Symbol(
                kind=_EMIT_KINDS[kind_kw], name=name, qualified_name=qualified,
                start_line=start + 1, end_line=j + 1,
                signature=block if len(block) <= 500 else block[:500] + "…",
                docstring=_docstring(lines, start),
                exported=True, language="prisma",
            ))
        i = j + 1

    return symbols
