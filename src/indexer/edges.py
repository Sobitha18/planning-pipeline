"""Symbol graph edges: imports/calls/inherits/implements, derived from the
already-indexed files+symbols. Two passes, orchestrated here; all tree-sitter
work and per-language resolution rules live in edges_python.py / edges_ts.py
— this module never branches on language beyond picking which of those two
modules to call.

Confidence is tiered, never faked:
- "resolved": same-file name match, or traced through a resolved import.
- "heuristic": name match within the same top-level package, <=5 candidates.
- >5 candidates, or no match at all -> dropped, no edge.

build_edges(repo_id) is a full rebuild (delete then reinsert) — the spec's
own "out of scope" list says incremental edge updates aren't needed for v1,
so idempotency comes for free instead of via upsert/ON CONFLICT bookkeeping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select

from src.db import get_session
from src.models import File, FileImport, Repo, Symbol, SymbolEdge

# ---------------------------------------------------------------- pass-1 data model
# Shared shapes both language modules parse into. Line numbers are 1-based,
# matching Symbol.start_line/end_line.


@dataclass
class RawImport:
    raw_specifier: str          # "./dashboard", "@/lib/prisma", "auth.jwt", "zod"
    names: list[str]            # imported local names; ["*"] for namespace/star
    line: int
    is_relative: bool = False   # python only: "from . import x" / "from .. import x"
    level: int = 0              # python only: number of leading dots


@dataclass
class RawCall:
    callee: str                 # bare name, or the attribute name of obj.method(...)
    receiver: str | None        # text of the receiver expr ("self"/"this"/"validator"); None for bare calls
    line: int


@dataclass
class RawBase:
    class_line: int             # start line of the class_definition/class_declaration
    base_name: str
    kind: str = "inherits"      # "inherits" (extends / python base) | "implements" (TS implements)


@dataclass
class FileParse:
    imports: list[RawImport] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    bases: list[RawBase] = field(default_factory=list)


# ---------------------------------------------------------------- stats


@dataclass
class EdgeStats:
    files_processed: int = 0
    edges_by_kind: dict[str, int] = field(default_factory=dict)
    edges_by_confidence: dict[str, int] = field(default_factory=dict)
    imports_total: int = 0
    imports_resolved: int = 0
    imports_external: int = 0       # bare/unaliased specifier — never expected to resolve
    imports_unresolved: int = 0     # looked internal (relative/aliased/known package) but no file matched
    errors: list[tuple[str, str]] = field(default_factory=list)


_PY_EXTS = {".py"}
_TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def _lang_of(path: str) -> str | None:
    ext = Path(path).suffix
    if ext in _PY_EXTS:
        return "python"
    if ext in _TS_EXTS:
        return "typescript"
    return None


# ---------------------------------------------------------------- symbol bookkeeping


@dataclass
class _Sym:
    id: int
    file_id: int
    path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int


def _pkg_of_path(path: str) -> str:
    """Top-level package name a file belongs to, for the same-package
    heuristic-match tier. 'src/auth/jwt.py' -> 'auth'; 'auth/jwt.py' -> 'auth'."""
    parts = path.split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    return parts[0] if parts else ""


def _enclosing(symbols: list[_Sym], line: int) -> _Sym | None:
    """Smallest symbol whose span contains `line` (innermost scope)."""
    best, best_span = None, None
    for s in symbols:
        if s.start_line <= line <= s.end_line:
            span = s.end_line - s.start_line
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def _occurs_in_span(lines: list[str], start_line: int, end_line: int, name: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    for ln in lines[max(0, start_line - 1):end_line]:
        if pattern.search(ln):
            return True
    return False


def _resolve_name(
    name: str, *, same_file: list[_Sym], imported_targets: dict[str, list[_Sym]],
    package: str, by_name: dict[str, list[_Sym]], pkg_of: dict[int, str],
) -> tuple[list[_Sym], str] | None:
    """Shared tiered resolution for bare callee names and base-class names."""
    same = [s for s in same_file if s.name == name]
    if same:
        return same, "resolved"
    if imported_targets.get(name):
        return imported_targets[name], "resolved"
    candidates = [s for s in by_name.get(name, []) if pkg_of.get(s.id) == package]
    if not candidates:
        return None
    if len(candidates) <= 5:
        return candidates, "heuristic"
    return None  # >5 repo-wide candidates -> drop, never fake precision


# ---------------------------------------------------------------- orchestration


def build_edges(repo_id: int) -> EdgeStats:
    from src.indexer import edges_python, edges_ts  # lazy: avoids import cycle with edges.py types

    stats = EdgeStats()
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            raise ValueError(f"no repo with id {repo_id}")

        files = session.execute(
            select(File.id, File.path, File.content).where(File.repo_id == repo_id)
        ).all()
        known_paths = {path for _, path, _ in files}

        sym_rows = session.execute(
            select(Symbol, File.path)
            .join(File, Symbol.file_id == File.id)
            .where(File.repo_id == repo_id)
        ).all()
        symbols_by_file: dict[int, list[_Sym]] = {}
        by_name: dict[str, list[_Sym]] = {}
        pkg_of: dict[int, str] = {}
        for sym, path in sym_rows:
            s = _Sym(
                id=sym.id, file_id=sym.file_id, path=path, name=sym.name,
                qualified_name=sym.qualified_name, kind=sym.kind,
                start_line=sym.start_line, end_line=sym.end_line,
            )
            symbols_by_file.setdefault(s.file_id, []).append(s)
            by_name.setdefault(s.name, []).append(s)
            pkg_of[s.id] = _pkg_of_path(path)
        path_to_file_id = {path: fid for fid, path, _ in files}

        alias_map = edges_ts.load_alias_map(Path(repo.root_path))
        top_pkgs = {_pkg_of_path(p) for p in known_paths}

        # full rebuild: clear this repo's rows before reinserting
        session.execute(delete(SymbolEdge).where(SymbolEdge.repo_id == repo_id))
        session.execute(
            delete(FileImport).where(FileImport.file_id.in_(select(File.id).where(File.repo_id == repo_id)))
        )

        file_import_rows: list[FileImport] = []
        edge_map: dict[tuple[int, int, str], str] = {}  # (from,to,kind) -> confidence

        def add_edge(from_id: int, to_id: int, kind: str, confidence: str) -> None:
            key = (from_id, to_id, kind)
            if edge_map.get(key) == "resolved":
                return
            edge_map[key] = confidence

        for file_id, path, content in files:
            lang = _lang_of(path)
            if lang is None:
                continue
            mod = edges_python if lang == "python" else edges_ts
            try:
                parsed = mod.parse_file(content)
            except Exception as e:  # noqa: BLE001 - one bad file must not fail the run
                stats.errors.append((path, str(e)))
                continue
            stats.files_processed += 1
            lines = content.splitlines()
            same_file = symbols_by_file.get(file_id, [])
            package = _pkg_of_path(path)

            # ---- imports (pass-1 collect + pass-2 resolve, per file) ----
            imported_targets: dict[str, list[_Sym]] = {}
            for imp in parsed.imports:
                stats.imports_total += 1
                if lang == "python":
                    looks_internal = imp.level > 0 or imp.raw_specifier.split(".")[0] in top_pkgs
                    target_path = edges_python.resolve_import(imp, path, known_paths)
                else:
                    looks_internal = imp.raw_specifier.startswith(".") or any(
                        imp.raw_specifier.startswith(p) for p in alias_map
                    )
                    target_path = edges_ts.resolve_import(imp, path, known_paths, alias_map)

                file_import_rows.append(FileImport(
                    file_id=file_id, imported_path=target_path,
                    raw_specifier=imp.raw_specifier, names=list(imp.names),
                ))
                if target_path is not None:
                    stats.imports_resolved += 1
                    target_symbols = symbols_by_file.get(path_to_file_id[target_path], [])
                    if imp.names == ["*"]:
                        for s in target_symbols:
                            imported_targets.setdefault(s.name, []).append(s)
                    else:
                        for n in imp.names:
                            matches = [s for s in target_symbols if s.name == n]
                            if matches:
                                imported_targets.setdefault(n, []).extend(matches)
                elif looks_internal:
                    stats.imports_unresolved += 1
                else:
                    stats.imports_external += 1

            # "imports" edges: which of this file's own symbols reference an
            # imported name in their body -> edge to the name's defining
            # symbol. Needs the full imported_targets for the file (built
            # above across all its import statements), so this runs after
            # the collect-and-resolve loop rather than inside it.
            for name, targets in imported_targets.items():
                for s in same_file:
                    if _occurs_in_span(lines, s.start_line, s.end_line, name):
                        for t in targets:
                            add_edge(s.id, t.id, "imports", "resolved")

            imported_classes = [s for lst in imported_targets.values() for s in lst if s.kind == "class"]

            # ---- calls ----
            for call in parsed.calls:
                from_sym = _enclosing(same_file, call.line)
                if from_sym is None:
                    continue

                if call.receiver in ("self", "this"):
                    enclosing_class = _enclosing(
                        [s for s in same_file if s.kind == "class"], call.line
                    )
                    if enclosing_class is None:
                        continue
                    target_qname = f"{enclosing_class.qualified_name}.{call.callee}"
                    matches = [s for s in same_file if s.qualified_name == target_qname]
                    for t in matches:
                        add_edge(from_sym.id, t.id, "calls", "resolved")
                    continue

                if call.receiver is not None:
                    # attribute call on a named receiver: no type inference (out of
                    # scope), so match structurally against classes imported into
                    # this file — if one of them has a same-named method, that's
                    # the resolved target regardless of the receiver's own name.
                    matched_any = False
                    for cls in imported_classes:
                        target_qname = f"{cls.qualified_name}.{call.callee}"
                        for t in by_name.get(call.callee, []):
                            if t.qualified_name == target_qname:
                                add_edge(from_sym.id, t.id, "calls", "resolved")
                                matched_any = True
                    if matched_any:
                        continue
                    # fall through to generic bare-name tiers below

                resolved = _resolve_name(
                    call.callee, same_file=same_file, imported_targets=imported_targets,
                    package=package, by_name=by_name, pkg_of=pkg_of,
                )
                if resolved is None:
                    continue
                targets, confidence = resolved
                for t in targets:
                    if t.id == from_sym.id:
                        continue
                    add_edge(from_sym.id, t.id, "calls", confidence)

            # ---- inherits / implements ----
            for base in parsed.bases:
                cls_sym = _enclosing([s for s in same_file if s.kind == "class"], base.class_line)
                if cls_sym is None:
                    continue
                resolved = _resolve_name(
                    base.base_name, same_file=same_file, imported_targets=imported_targets,
                    package=package, by_name=by_name, pkg_of=pkg_of,
                )
                if resolved is None:
                    continue
                targets, confidence = resolved
                for t in targets:
                    if t.kind != "class" or t.id == cls_sym.id:
                        continue
                    add_edge(cls_sym.id, t.id, base.kind, confidence)

        session.add_all(file_import_rows)
        for (from_id, to_id, kind), confidence in edge_map.items():
            session.add(SymbolEdge(
                repo_id=repo_id, from_symbol=from_id, to_symbol=to_id,
                kind=kind, confidence=confidence,
            ))
            stats.edges_by_kind[kind] = stats.edges_by_kind.get(kind, 0) + 1
            stats.edges_by_confidence[confidence] = stats.edges_by_confidence.get(confidence, 0) + 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return stats
