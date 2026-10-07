"""Internal query API over the stored index. Read-only.

Stage A (context retrieval) imports ONLY this module — no SQL outside
db.py / index_query.py / indexer/full_index.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from sqlalchemy import func, or_, select

from src.db import get_session
from src.models import File, FileImport, ProjectRepo, Repo, Symbol, SymbolEdge


@dataclass
class SymbolHit:
    id: int          # needed to feed neighbors(); every other field is display
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str
    docstring: str | None
    exported: bool
    path: str


@dataclass
class FileHit:
    path: str
    score: float
    snippet: str


def resolve_repo_id(root_path: str) -> int | None:
    session = get_session()
    try:
        repo = session.execute(
            select(Repo).where(Repo.root_path == str(Path(root_path).resolve()))
        ).scalar_one_or_none()
        return repo.id if repo else None
    finally:
        session.close()


def get_repo(repo_id) -> dict | None:
    """Root path + pinned SHA of an indexed repo (Stage A needs both:
    `git log` runs in the working tree, the pack records the SHA)."""
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        return None if repo is None else {
            "id": repo.id, "root_path": repo.root_path, "indexed_sha": repo.indexed_sha,
        }
    finally:
        session.close()


def get_project_repos(project_id: int) -> list[dict]:
    """Every repo in a project, ordered by id (stable ordering keeps the
    router's repo names deterministic)."""
    session = get_session()
    try:
        q = (
            select(Repo)
            .join(ProjectRepo, ProjectRepo.repo_id == Repo.id)
            .where(ProjectRepo.project_id == project_id)
            .order_by(Repo.id)
        )
        return [
            {"id": r.id, "root_path": r.root_path, "status": r.status,
             "indexed_sha": r.indexed_sha}
            for r in session.execute(q).scalars()
        ]
    finally:
        session.close()


def repo_stats(repo_id) -> dict:
    """Shape of an indexed repo: file count, files per language, and the
    top-level directories (with file counts). Index-only — no disk access."""
    session = get_session()
    try:
        rows = session.execute(
            select(File.path, File.language).where(File.repo_id == repo_id)
        ).all()
    finally:
        session.close()
    languages: dict[str, int] = {}
    top_dirs: dict[str, int] = {}
    for path, language in rows:
        languages[language] = languages.get(language, 0) + 1
        head = path.split("/", 1)[0] if "/" in path else "."
        top_dirs[head] = top_dirs.get(head, 0) + 1
    return {"file_count": len(rows), "languages": languages, "top_dirs": top_dirs}


def exported_symbol_names(repo_id, limit: int = 30) -> list[str]:
    session = get_session()
    try:
        q = (
            select(Symbol.qualified_name)
            .join(File, Symbol.file_id == File.id)
            .where(File.repo_id == repo_id, Symbol.exported.is_(True))
            .order_by(Symbol.qualified_name)
            .limit(limit)
        )
        return [n for (n,) in session.execute(q).all()]
    finally:
        session.close()


# Mirrors src/indexer/tables.TABLE_KINDS (not imported: query layer stays
# free of indexer imports): where a table is defined, changed, or a Prisma model.
TABLE_KINDS = ("table", "table_change", "model")


def find_tables(repo_id, name: str | None = None, *, limit: int = 40) -> list[SymbolHit]:
    """Table symbols in a repo (definitions first, then migration changes);
    `name` narrows to a case-insensitive substring of the table name."""
    session = get_session()
    try:
        q = (
            select(Symbol, File.path)
            .join(File, Symbol.file_id == File.id)
            .where(File.repo_id == repo_id, Symbol.kind.in_(TABLE_KINDS))
        )
        if name:
            q = q.where(Symbol.name.ilike(f"%{_escaped(name)}%", escape="\\"))
        q = q.order_by(Symbol.kind == "table_change", Symbol.name, File.path).limit(limit)
        return [_symbol_hit(sym, path) for sym, path in session.execute(q).all()]
    finally:
        session.close()


def _escaped(s: str) -> str:
    """Escape LIKE/ILIKE wildcards so a literal name search stays literal."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _symbol_hit(sym: Symbol, path: str) -> SymbolHit:
    return SymbolHit(
        id=sym.id, kind=sym.kind, name=sym.name, qualified_name=sym.qualified_name,
        start_line=sym.start_line, end_line=sym.end_line,
        signature=sym.signature, docstring=sym.docstring,
        exported=sym.exported, path=path,
    )


def _snippet(content: str, query: str, context: int = 1) -> str:
    """~3-line window around the matched region: try the literal phrase
    first (trigram case), fall back to any query word (fts case)."""
    lines = content.splitlines()
    ql = query.lower()
    idx = next((i for i, ln in enumerate(lines) if ql in ln.lower()), None)
    if idx is None:
        words = [w.lower() for w in query.split() if w]
        idx = next(
            (i for i, ln in enumerate(lines) if any(w in ln.lower() for w in words)),
            None,
        )
    if idx is None:
        return "\n".join(lines[:3])
    lo, hi = max(0, idx - context), min(len(lines), idx + context + 1)
    return "\n".join(lines[lo:hi])


def find_symbols(repo_id, name: str, *, fuzzy: bool = False,
                  kind: str | None = None, exported_only: bool = False,
                  limit: int = 20) -> list[SymbolHit]:
    session = get_session()
    try:
        q = select(Symbol, File.path).join(File, Symbol.file_id == File.id)
        q = q.where(File.repo_id == repo_id)
        if fuzzy:
            like = f"%{_escaped(name)}%"
            q = q.where(
                Symbol.name.ilike(like, escape="\\")
                | Symbol.qualified_name.ilike(like, escape="\\")
            )
        else:
            q = q.where(
                (Symbol.name == name)
                | Symbol.qualified_name.endswith(name, autoescape=True)
            )
        if kind:
            q = q.where(Symbol.kind == kind)
        if exported_only:
            q = q.where(Symbol.exported.is_(True))
        q = q.order_by(Symbol.qualified_name).limit(limit)
        return [_symbol_hit(sym, path) for sym, path in session.execute(q).all()]
    finally:
        session.close()


def search_text(repo_id, query: str, *, mode: str = "fts",
                 limit: int = 10) -> list[FileHit]:
    session = get_session()
    try:
        if mode == "fts":
            tsquery = func.websearch_to_tsquery("simple", query)
            rank = func.ts_rank(File.content_tsv, tsquery)
            q = (
                select(File.path, File.content, rank)
                .where(File.repo_id == repo_id, File.content_tsv.op("@@")(tsquery))
                .order_by(rank.desc())
                .limit(limit)
            )
        elif mode == "trigram":
            sim = func.similarity(File.content, query)
            q = (
                select(File.path, File.content, sim)
                .where(
                    File.repo_id == repo_id,
                    File.content.ilike(f"%{_escaped(query)}%", escape="\\"),
                )
                .order_by(sim.desc())
                .limit(limit)
            )
        else:
            raise ValueError(f"unknown search_text mode: {mode!r}")

        return [
            FileHit(path=path, score=float(score), snippet=_snippet(content, query))
            for path, content, score in session.execute(q).all()
        ]
    finally:
        session.close()


def get_file(repo_id, path: str) -> str | None:
    session = get_session()
    try:
        return session.execute(
            select(File.content).where(File.repo_id == repo_id, File.path == path)
        ).scalar_one_or_none()
    finally:
        session.close()


def list_paths(repo_id) -> list[str]:
    """Every indexed path in the repo. Path validity (task 07) checks a whole
    plan's worth of paths at once — one query beats a get_file per path."""
    session = get_session()
    try:
        return [
            p for (p,) in session.execute(
                select(File.path).where(File.repo_id == repo_id).order_by(File.path)
            ).all()
        ]
    finally:
        session.close()


def get_symbols_in_file(repo_id, path: str) -> list[SymbolHit]:
    session = get_session()
    try:
        q = (
            select(Symbol, File.path)
            .join(File, Symbol.file_id == File.id)
            .where(File.repo_id == repo_id, File.path == path)
            .order_by(Symbol.start_line)
        )
        return [_symbol_hit(sym, p) for sym, p in session.execute(q).all()]
    finally:
        session.close()


# Path conventions, both ecosystems. '%' is a real wildcard; a stray literal
# '_' in these fixed patterns just makes matching a shade broader, never
# narrower, so it's left unescaped here.
_TEST_PATH_PATTERNS = (
    "%.test.%", "%.spec.%", "%__tests__%",
    "%test_%.py", "%_test.py", "%tests/%", "%conftest.py",
)


def is_test_path(path: str) -> bool:
    """Same conventions as _TEST_PATH_PATTERNS, evaluated in Python instead of
    SQL (task 07's test_coverage check has no DB row to match against)."""
    return any(fnmatch(path, pat.replace("%", "*")) for pat in _TEST_PATH_PATTERNS)


def neighbors(repo_id, symbol_ids: list[int], *, direction: str = "both",
              kinds: list[str] | None = None, min_confidence: str = "heuristic",
              limit: int = 50) -> list[SymbolHit]:
    """1-hop symbol-graph expansion: callers/callees of symbol_ids (any edge
    kind — imports/calls/inherits/implements — unless `kinds` narrows it)."""
    if not symbol_ids:
        return []
    confidences = ("resolved",) if min_confidence == "resolved" else ("resolved", "heuristic")

    def _query(edge_col, other_col):
        q = (
            select(Symbol, File.path)
            .join(SymbolEdge, other_col == Symbol.id)
            .join(File, Symbol.file_id == File.id)
            .where(
                SymbolEdge.repo_id == repo_id,
                edge_col.in_(symbol_ids),
                SymbolEdge.confidence.in_(confidences),
            )
        )
        if kinds:
            q = q.where(SymbolEdge.kind.in_(kinds))
        return q.limit(limit)

    session = get_session()
    try:
        queries = []
        if direction in ("callees", "both"):
            queries.append(_query(SymbolEdge.from_symbol, SymbolEdge.to_symbol))
        if direction in ("callers", "both"):
            queries.append(_query(SymbolEdge.to_symbol, SymbolEdge.from_symbol))

        seen: set[int] = set()
        hits: list[SymbolHit] = []
        for q in queries:
            for sym, path in session.execute(q).all():
                if sym.id in seen:
                    continue
                seen.add(sym.id)
                hits.append(_symbol_hit(sym, path))
        return hits[:limit]
    finally:
        session.close()


def imports_of_file(repo_id, path: str) -> list[dict]:
    session = get_session()
    try:
        q = (
            select(FileImport)
            .join(File, FileImport.file_id == File.id)
            .where(File.repo_id == repo_id, File.path == path)
        )
        return [
            {"imported_path": fi.imported_path, "raw_specifier": fi.raw_specifier, "names": fi.names}
            for fi in session.execute(q).scalars()
        ]
    finally:
        session.close()


def importers_of_file(repo_id, path: str) -> list[str]:
    session = get_session()
    try:
        q = (
            select(File.path)
            .join(FileImport, FileImport.file_id == File.id)
            .where(File.repo_id == repo_id, FileImport.imported_path == path)
            .distinct()
        )
        return [p for (p,) in session.execute(q).all()]
    finally:
        session.close()


def tests_of(repo_id, symbol_names: list[str], limit: int = 10) -> list[FileHit]:
    if not symbol_names:
        return []
    session = get_session()
    try:
        path_match = or_(*(File.path.ilike(pat) for pat in _TEST_PATH_PATTERNS))
        name_match = or_(
            *(File.content.ilike(f"%{_escaped(n)}%", escape="\\") for n in symbol_names)
        )
        q = (
            select(File.path, File.content)
            .where(File.repo_id == repo_id, path_match, name_match)
            .limit(limit)
        )
        return [
            FileHit(path=path, score=1.0, snippet=_snippet(content, symbol_names[0]))
            for path, content in session.execute(q).all()
        ]
    finally:
        session.close()
