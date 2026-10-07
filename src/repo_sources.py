"""Turn a user-supplied repo reference (a git URL or an absolute path) into a
directory on this machine that can be indexed.

- absolute path      -> used in place, must be an existing directory.
- git URL            -> cloned under REPOS_DIR/<host>/<owner>/<repo>; if that
                        directory already holds a clone of the same remote it is
                        fast-forwarded instead of re-cloned.

Accepted URL shapes: https://host/owner/repo(.git), ssh://[user@]host/owner/repo(.git),
git@host:owner/repo(.git), file:///abs/path/repo(.git). Plain http:// and anything
else is rejected.

git runs non-interactively (a missing credential fails fast instead of hanging
the request on a prompt) and every argument is passed as a list, with `--`
before the URL, so a hostile string can't inject options or shell commands.
Credentials are whatever git/gh already has configured for this user.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.config import get_settings
from src.runs import DomainError

GIT_TIMEOUT_S = 900

_SCP_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[\w./~-]+)$")  # git@host:owner/repo
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class RepoSourceError(DomainError):
    """The reference isn't a usable path/URL, or git failed to fetch it."""


@dataclass(frozen=True)
class RepoSource:
    kind: str            # "path" | "url"
    raw: str
    path: Path | None = None            # kind == "path": resolved directory
    url: str | None = None              # kind == "url"
    rel_dir: Path | None = None         # kind == "url": <host>/<owner>/<repo>


@dataclass(frozen=True)
class Resolved:
    root: Path
    action: str          # "local" | "cloned" | "updated"


def _clean(segment: str) -> str:
    return _SAFE.sub("_", segment).strip(".") or "_"


def _url_parts(url: str) -> tuple[str, list[str]]:
    """(host, [path segments without the .git suffix]) for any accepted URL."""
    if url.startswith(("https://", "ssh://", "file://")):
        rest = url.split("://", 1)[1]
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1] if not url.startswith("file://") else "local"
        if url.startswith("file://"):
            path = rest.lstrip("/")
    else:
        m = _SCP_RE.match(url)
        if not m:
            raise RepoSourceError(f"{url!r} is not a supported git URL")
        host, path = m.group("host"), m.group("path")
    segments = [s for s in path.strip("/").split("/") if s]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
    if not segments or not segments[-1]:
        raise RepoSourceError(f"{url!r} does not name a repository")
    return host, segments


def classify(raw: str) -> RepoSource:
    raw = (raw or "").strip()
    if not raw:
        raise RepoSourceError("empty repo reference")
    if raw.startswith("-"):
        raise RepoSourceError(f"{raw!r} is not a repo path or URL")

    if raw.startswith(("/", "~")):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise RepoSourceError(f"{raw!r}: path must be absolute")
        if not path.is_dir():
            raise RepoSourceError(f"{raw!r} does not exist or is not a directory")
        return RepoSource(kind="path", raw=raw, path=path.resolve())

    if raw.startswith("http://"):
        raise RepoSourceError(f"{raw!r}: plain http:// is not allowed, use https://")
    if "://" in raw and not raw.startswith(("https://", "ssh://", "file://")):
        raise RepoSourceError(f"{raw!r}: unsupported URL scheme (use https://, ssh://, git@host:... or file://)")
    if raw.startswith(("https://", "ssh://", "file://")) or _SCP_RE.match(raw):
        host, segments = _url_parts(raw)
        rel = Path(_clean(host), *(_clean(s) for s in segments[-2:]))
        return RepoSource(kind="url", raw=raw, url=raw, rel_dir=rel)

    raise RepoSourceError(
        f"{raw!r} is neither an absolute path nor a git URL "
        "(https://..., git@host:owner/repo, ssh://..., file:///...)"
    )


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RepoSourceError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from None
    except FileNotFoundError:
        raise RepoSourceError("git is not installed on this machine") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RepoSourceError(f"git {args[0]} failed: {detail[-1] if detail else 'unknown error'}")
    return proc.stdout.strip()


def _same_remote(a: str, b: str) -> bool:
    norm = lambda u: u.strip().rstrip("/").removesuffix(".git").lower()  # noqa: E731
    return norm(a) == norm(b)


def repos_dir() -> Path:
    return Path(get_settings().repos_dir).expanduser().resolve()


def resolve(source: RepoSource) -> Resolved:
    """Make `source` available on disk; clone or fast-forward for URLs."""
    if source.kind == "path":
        return Resolved(root=source.path, action="local")

    dest = repos_dir() / source.rel_dir
    if (dest / ".git").exists():
        origin = _git(["remote", "get-url", "origin"], cwd=dest)
        if not _same_remote(origin, source.url):
            raise RepoSourceError(
                f"{dest} already holds a clone of {origin}, not {source.url}"
            )
        _git(["pull", "--ff-only"], cwd=dest)
        return Resolved(root=dest.resolve(), action="updated")

    if dest.exists() and any(dest.iterdir()):
        raise RepoSourceError(f"{dest} exists and is not a git clone; refusing to overwrite it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--", source.url, str(dest)])
    return Resolved(root=dest.resolve(), action="cloned")
