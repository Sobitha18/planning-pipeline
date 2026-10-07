"""File filtering: what should_index lets through the walk in full_index.py."""

from __future__ import annotations

from pathlib import Path

import pathspec

from src.indexer.extractor import SUPPORTED_EXTENSIONS

SKIP_DIRS = {
    "node_modules", ".next", ".git", "dist", "build", ".turbo", "coverage",
    ".vercel", "out", "__pycache__", ".venv", "venv", "env", ".tox",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages",
    ".eggs", ".worktrees",
}
# "migrations" is deliberately NOT skipped: migration files are indexed as
# table symbols only (see tables.py), which is how the repo router can tell
# which repo owns a schema.

SKIP_SUFFIXES = (".d.ts", ".min.js")

MAX_BYTES = 1_000_000
MAX_LINES = 5000


def _gitignore_spec(repo_root: Path) -> pathspec.PathSpec | None:
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return None
    return pathspec.PathSpec.from_lines(
        "gitwildmatch", gitignore.read_text(errors="replace").splitlines()
    )


def should_index(path: Path, repo_root: Path) -> bool:
    if path.suffix not in SUPPORTED_EXTENSIONS:
        return False

    if any(part in SKIP_DIRS for part in path.parts):
        return False

    name = path.name
    if any(name.endswith(suf) for suf in SKIP_SUFFIXES):
        return False

    try:
        if path.stat().st_size > MAX_BYTES:
            return False
    except OSError:
        return False
    try:
        with path.open("r", errors="replace") as f:
            for i, _ in enumerate(f, 1):
                if i > MAX_LINES:
                    return False
    except OSError:
        return False

    spec = _gitignore_spec(repo_root)
    if spec is not None:
        rel = path.relative_to(repo_root).as_posix()
        if spec.match_file(rel):
            return False

    return True
