"""Interactive front door: name a project, give it repos (git URLs or absolute
paths), then type feature requests and see which repos each one touches.

    python -m src.cli                       # asks for everything
    python -m src.cli shop --repo URL --repo /abs/path -q "add coupons"

Runs in-process (no server): clones, indexes and routes right here, printing
progress. Re-running with an existing project name reuses it and its indexed
repos; give --repo / paste more repos to add to it.

Reads DATABASE_URL and LLM_API_KEY from the environment or a `.env` in the
current directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from src import projects, repo_router
from src.runs import DomainError

PROMPT_REPOS = ("Repos: paste git URLs (https://..., git@host:owner/repo) or absolute paths,\n"
                "one per line. Empty line when done.")


def _index_now(repo_id: int, out: Callable[[str], None]) -> None:
    """Index one repo to completion (the API does this on a thread pool)."""
    from src.db import get_session
    from src.indexer.edges import build_edges
    from src.indexer.full_index import index_repo
    from src.models import Repo

    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        root = repo.root_path
    finally:
        session.close()
    out(f"  indexing {root} ...")
    result = index_repo(root)
    build_edges(repo_id)
    out(f"    {result.files_indexed + result.files_skipped_unchanged} files, "
        f"{result.symbols_count} symbols" + (f", {len(result.errors)} file errors" if result.errors else ""))


def format_selection(selection) -> str:
    lines = []
    for role in ("primary", "impacted"):
        for r in (x for x in selection.repos if x.role == role):
            lines.append(f"\n{role.upper():9} {r.repo}")
            lines.append(f"  why:      {r.reason}")
            for ev in r.evidence:
                lines.append(f"  evidence: {ev.path} - {ev.note}")
    s = selection.stats
    lines.append(f"\n({s.get('turns')} model turns, {s.get('tool_calls')} lookups"
                 f"{', answer was repaired' if s.get('repaired') else ''}"
                 f"{', hit the lookup limit' if s.get('exhausted') else ''})")
    for d in s.get("dropped_evidence") or []:
        lines.append(f"note: dropped an invalid citation in {d['repo']}: {d.get('file') or d.get('reason')}")
    return "\n".join(lines)


def _read_repos(ask: Callable[[str], str], out: Callable[[str], None]) -> list[str]:
    out(PROMPT_REPOS)
    repos = []
    while True:
        line = ask("> ").strip()
        if not line:
            return repos
        repos.append(line)


REQUEST_PROMPT = "\nFeature request (paste it or type it; empty line to quit):\n> "
PASTE_GRACE_S = 0.3


def _drain_pasted_lines() -> list[str]:
    """Lines that arrived together with the one input() just returned.

    A terminal delivers a paste all at once, so every line after the first is
    already waiting on stdin when input() returns; typing never is. Waiting a
    short grace period for more input therefore separates "pasted a multi-line
    spec" from "typed one line", with no end marker needed.
    """
    import select

    lines: list[str] = []
    while select.select([sys.stdin], [], [], PASTE_GRACE_S)[0]:
        line = sys.stdin.readline()
        if not line:                      # EOF
            break
        lines.append(line.rstrip("\r\n"))
    return lines


def _read_request(ask: Callable[[str], str],
                  pasted: Callable[[], list[str]] = lambda: []) -> str | None:
    """One request, possibly many lines. A typed line is the whole request; a
    paste (everything `pasted()` finds already waiting after the first line) is
    joined into one. A trailing '.' line, left over from the old end marker, is
    dropped. An empty first line, or end of input, quits (None).
    """
    try:
        first = ask(REQUEST_PROMPT)
    except EOFError:        # Ctrl-D / piped input exhausted at the prompt: quit cleanly
        return None
    if not first.strip():
        return None
    lines = [first, *pasted()]
    if lines[-1].strip() == ".":
        return "\n".join(lines[:-1]).strip()
    if len(lines) > 1:      # a paste: it is complete, don't wait for more
        return "\n".join(lines).strip()
    return first.strip()


def run(argv: list[str] | None, ask: Callable[[str], str] = input,
        out: Callable[[str], None] = print) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m src.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("name", nargs="?", help="project name (asked if omitted)")
    parser.add_argument("--repo", "-r", action="append", default=[], help="git URL or absolute path; repeatable")
    parser.add_argument("--request", "-q", help="route this one request and exit (otherwise interactive)")
    parser.add_argument("--request-file", "-f", help="like -q, reading the request text from a file")
    args = parser.parse_args(argv)
    if args.request_file:
        args.request = Path(args.request_file).read_text()

    if not os.environ.get("DATABASE_URL"):
        out("DATABASE_URL is not set, e.g.\n  export DATABASE_URL=postgresql+psycopg://<user>@localhost:5432/ppl")
        return 2

    from src.db import get_session, init_schema
    init_schema()

    name = (args.name or ask("Project name: ")).strip()
    if not name:
        out("A project name is required.")
        return 2

    session = get_session()
    try:
        project = projects.get_project_by_name(session, name)
        new_repos = list(args.repo)
        if project is not None:
            existing = projects.describe(session, project)["repos"]
            out(f"Project {name!r} already exists with {len(existing)} repo(s).")
            if not new_repos and args.request is None:
                out("Add more repos? " + PROMPT_REPOS.split("\n")[0].replace("Repos: ", ""))
                new_repos = _read_repos(ask, out)
        else:
            if not new_repos:
                new_repos = _read_repos(ask, out)
            if not new_repos:
                out("A new project needs at least one repo.")
                return 2

        try:
            if new_repos:
                out("Fetching and indexing repos (cloning can take a while) ...")
                if project is None:
                    projects.ensure_name_free(session, name)
                added = projects.register_sources(session, new_repos, lambda rid: _index_now(rid, out))
                if project is None:
                    project = projects.create_project(session, name, [a["repo_id"] for a in added])
                    out(f"Created project {name!r}.")
                else:
                    for a in added:
                        projects.add_repo(session, project.id, a["repo_id"])
                for a in added:
                    out(f"  {a['action']:7} {a['root_path']}" + (f"  [{a['warning']}]" if "warning" in a else ""))
            projects.require_ready(project.id)
        except DomainError as exc:
            out(f"Error: {exc}")
            return 1
        project_id = project.id
    finally:
        session.close()

    if not os.environ.get("LLM_API_KEY"):
        out("LLM_API_KEY is not set; repos are indexed, but routing needs it:\n  export LLM_API_KEY=sk-ant-...")
        return 2

    # Only a real terminal can deliver a paste; injected `ask`s (tests) never do.
    pasted = _drain_pasted_lines if ask is input and sys.stdin.isatty() else (lambda: [])

    def route(text: str) -> bool:
        out("Investigating ...")
        try:
            out(format_selection(repo_router.select_repos(project_id, text)))
            return True
        except (DomainError, repo_router.RepoSelectionError) as exc:
            out(f"Error: {exc}")
            return False

    if args.request is not None:
        return 0 if route(args.request) else 1
    while True:
        text = _read_request(ask, pasted)
        if text is None:
            return 0
        route(text)


def main() -> None:
    try:
        sys.exit(run(sys.argv[1:]))
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
