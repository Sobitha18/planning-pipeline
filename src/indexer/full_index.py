"""Walk a repo, run the extractor on every supported file, store in Postgres.

Idempotent: re-running is the update mechanism (no incremental git-diff yet).
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.db import get_session
from src.indexer.extractor import extract
from src.indexer.ignore import should_index
from src.models import File, Repo, Symbol

# Mirrors extractor._LANGUAGES; not exported there, so kept in sync by hand —
# every extension the extractor supports needs a language label here too.
_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript", ".js": "typescript",
    ".jsx": "typescript", ".mjs": "typescript", ".cjs": "typescript",
    ".prisma": "prisma", ".sql": "sql",
}

BATCH_SIZE = 200


@dataclass
class IndexResult:
    files_indexed: int = 0
    files_skipped_unchanged: int = 0
    files_deleted: int = 0
    symbols_count: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    duration_s: float = 0.0


def _git_head_sha(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def index_repo(root_path: str) -> IndexResult:
    root = Path(root_path).resolve()
    started = time.monotonic()
    result = IndexResult()

    session = get_session()
    repo: Repo | None = None
    try:
        repo = session.execute(
            select(Repo).where(Repo.root_path == str(root))
        ).scalar_one_or_none()
        if repo is None:
            repo = Repo(root_path=str(root), status="building")
            session.add(repo)
        else:
            repo.status = "building"
        session.commit()

        sha = _git_head_sha(root)

        disk_files = sorted(
            (p for p in root.rglob("*") if p.is_file() and should_index(p, root)),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        disk_paths = {p.relative_to(root).as_posix() for p in disk_files}

        existing = {
            f.path: f
            for f in session.execute(
                select(File).where(File.repo_id == repo.id)
            ).scalars()
        }

        # files gone from disk (deleted, or now ignore-rule-excluded)
        to_delete = [f for rel, f in existing.items() if rel not in disk_paths]
        for f in to_delete:
            session.delete(f)
        result.files_deleted = len(to_delete)
        if to_delete:
            session.commit()

        batch: list[File] = []
        for p in disk_files:
            rel = p.relative_to(root).as_posix()
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                result.errors.append((rel, str(e)))
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            prior = existing.get(rel)
            if prior is not None and prior.content_hash == content_hash:
                result.files_skipped_unchanged += 1
                continue

            try:
                symbols = extract(rel, content, repo_root=str(root))
            except Exception as e:  # noqa: BLE001 - one bad file must not fail the run
                result.errors.append((rel, str(e)))
                continue

            if prior is not None:
                session.delete(prior)  # cascades to its old symbols
                session.flush()

            db_file = File(
                repo_id=repo.id, path=rel,
                language=_LANG_BY_EXT.get(p.suffix, "unknown"),
                content_hash=content_hash,
                loc=len(content.splitlines()),
                content=content,
            )
            db_file.symbols = [
                Symbol(
                    kind=s.kind, name=s.name, qualified_name=s.qualified_name,
                    start_line=s.start_line, end_line=s.end_line,
                    signature=s.signature, docstring=s.docstring,
                    exported=s.exported,
                )
                for s in symbols
            ]
            batch.append(db_file)
            result.files_indexed += 1
            result.symbols_count += len(symbols)

            if len(batch) >= BATCH_SIZE:
                session.add_all(batch)
                session.commit()
                batch = []

        if batch:
            session.add_all(batch)
            session.commit()

        repo.status = "ready"
        repo.indexed_sha = sha
        repo.indexed_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        if repo is not None:
            repo.status = "failed"
            session.commit()
        raise
    finally:
        result.duration_s = time.monotonic() - started
        session.close()

    return result
