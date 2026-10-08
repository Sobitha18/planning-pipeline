"""Repo router: given a project (a set of indexed repos) and a request, decide
which repos the request touches — `primary` (where the change is made) and
`impacted` (depend on what changes) — before any per-repo planning happens.

An agent does the investigating: it gets read-only tools over the index
(search / symbols / files / imports) and works out how the repos relate on its
own, so nothing here knows about any particular linking style (packages, HTTP,
queues, shared DB...). Code only bounds and checks it, same rule as the rest of
the pipeline: the LLM proposes over a closed set (the project's repos, files
that tools actually showed it), code disposes (validate_answer).

Evidence is cited by file NUMBER, never by path. Every file a tool shows gets a
number ([F12]) in an EvidenceRegistry; the agent's answer cites numbers and the
code writes the real paths itself, so a mistyped or half-remembered path cannot
reach the result.

All index reads go through src.index_query; the only other I/O is reading a
repo's README / manifest files from disk for its profile (the index holds code
files only).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from src import index_query as iq
from src.config import get_settings
from src.llm import LLMGateway, TokenBudget, load_prompt
from src.models import (
    AgentAnswer,
    AgentRuledOut,
    RepoEvidence,
    RepoSelection,
    ResolvedRepo,
    RuledOutRepo,
)

MAX_TOOL_OUTPUT_CHARS = 6000
SEARCH_HITS_PER_REPO = 5
README_CHARS = 600
MANIFEST_CHARS = 800

# Package / build manifests worth showing the agent. Language-agnostic list;
# a repo with none of these simply has no manifest section.
MANIFEST_NAMES = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod",
    "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
    "Gemfile", "requirements.txt",
)


class RepoSelectionError(Exception):
    """The router could not produce a valid selection (even after one repair)."""


# ------------------------------------------------------------------ naming


def repo_names(repos: list[dict]) -> dict[str, dict]:
    """name -> repo dict. Name is the root dir's basename; a clash gets the id
    appended so every name is unique and stable."""
    counts: dict[str, int] = {}
    for r in repos:
        base = Path(r["root_path"]).name
        counts[base] = counts.get(base, 0) + 1
    out: dict[str, dict] = {}
    for r in repos:
        base = Path(r["root_path"]).name
        out[base if counts[base] == 1 else f"{base}-{r['id']}"] = r
    return out


# ----------------------------------------------------------------- profile


def _read_head(path: Path, chars: int) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:chars]
    except OSError:
        return None


def build_profile(name: str, repo: dict) -> dict:
    """Orientation card for one repo: shape from the index, plus README and
    manifest heads from disk (missing root / files are just skipped)."""
    stats = iq.repo_stats(repo["id"])
    profile = {
        "name": name,
        "status": repo["status"],
        "file_count": stats["file_count"],
        "languages": stats["languages"],
        "top_dirs": stats["top_dirs"],
        "exported_symbols": iq.exported_symbol_names(repo["id"], limit=15),
    }
    # Which tables this repo defines / migrates: how the agent learns who owns
    # a schema. Names only; find_tables gives the detail.
    defined, changed = [], []
    for t in iq.find_tables(repo["id"], limit=60):
        bucket = changed if t.kind == "table_change" else defined
        if t.name not in bucket:
            bucket.append(t.name)
    if defined:
        profile["tables_defined"] = defined
    if changed:
        profile["tables_changed_by_migrations"] = [t for t in changed if t not in defined]
    root = Path(repo["root_path"])
    if root.is_dir():
        readme = next(
            (p for p in sorted(root.iterdir()) if p.is_file() and p.name.lower().startswith("readme")),
            None,
        )
        if readme is not None:
            profile["readme"] = _read_head(readme, README_CHARS)
        manifests = {}
        for manifest in MANIFEST_NAMES:
            head = _read_head(root / manifest, MANIFEST_CHARS)
            if head is not None:
                manifests[manifest] = head
        if manifests:
            profile["manifests"] = manifests
    return profile


# ---------------------------------------------------------- file numbering


class EvidenceRegistry:
    """File number <-> (repo name, path), for one router run. A file keeps the
    same number every time a tool shows it."""

    def __init__(self) -> None:
        self._by_file: dict[tuple[str, str], str] = {}
        self._by_id: dict[str, tuple[str, str]] = {}

    def ref(self, repo: str, path: str) -> str:
        key = (repo, path)
        if key not in self._by_file:
            fid = f"F{len(self._by_file) + 1}"
            self._by_file[key] = fid
            self._by_id[fid] = key
        return self._by_file[key]

    def get(self, file_id: str) -> tuple[str, str] | None:
        return self._by_id.get(file_id.strip().upper())

    def listing(self, limit: int = 400) -> str:
        """Every citable file, for the repair prompt."""
        rows = [f"{fid}  {repo}  {path}" for fid, (repo, path) in list(self._by_id.items())[:limit]]
        extra = len(self._by_id) - limit
        return "\n".join(rows) + (f"\n[+{extra} more not listed]" if extra > 0 else "")

    def __len__(self) -> int:
        return len(self._by_id)


# ------------------------------------------------------------------- tools


def _repo_prop(description: str = "Repo name from list_repos") -> dict:
    return {"type": "string", "description": description}


TOOLS = [
    {
        "name": "list_repos",
        "description": "Every repo in the project with a profile: name, languages, top-level "
                       "dirs, README start, package/build manifests, exported symbols.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_code",
        "description": "Search indexed code. mode 'fts' = word search (websearch syntax); "
                       "'literal' = exact substring (use for route paths, table/event/package "
                       "names, error strings). Omit `repo` to search ALL repos; hits are grouped "
                       "by repo. Every file shown carries a number like [F12] you cite as evidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo": _repo_prop("Limit to one repo (optional)"),
                "mode": {"type": "string", "enum": ["fts", "literal"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_symbol",
        "description": "Find functions/classes/types by name (substring match). Omit `repo` "
                       "to look in all repos. Files carry [F#] numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "repo": _repo_prop("optional")},
            "required": ["name"],
        },
    },
    {
        "name": "find_tables",
        "description": "Database tables/models the repos define or migrate: SQL CREATE TABLE, ORM "
                       "models, Prisma models (kind table/model) and migrations that alter a table "
                       "(kind table_change). Optional `name` substring; omit `repo` for all repos. "
                       "Use it to see which repo owns a schema and which repos touch a table. "
                       "Files carry [F#] numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "repo": _repo_prop("optional")},
        },
    },
    {
        "name": "list_files",
        "description": "Indexed file paths in a repo, optionally under a path prefix. Files carry [F#] numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": _repo_prop(), "prefix": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "file_outline",
        "description": "Symbols (name, kind, signature, line) defined in one file.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": _repo_prop(), "path": {"type": "string"}},
            "required": ["repo", "path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read part of a file (default first 120 lines).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": _repo_prop(), "path": {"type": "string"},
                "start_line": {"type": "integer"}, "max_lines": {"type": "integer"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "imports_of",
        "description": "What a file imports (raw specifier, resolved in-repo path or null for "
                       "external, imported names). External specifiers can name other repos' packages.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": _repo_prop(), "path": {"type": "string"}},
            "required": ["repo", "path"],
        },
    },
    {
        "name": "importers_of",
        "description": "Files in the same repo that import the given file. Files carry [F#] numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": _repo_prop(), "path": {"type": "string"}},
            "required": ["repo", "path"],
        },
    },
]


def _cap(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + "\n[truncated]"


class RouterTools:
    """Executes the tool calls for one project. Every file a tool mentions is
    registered and printed as `[F#] path`; the model cites the number."""

    def __init__(self, repos_by_name: dict[str, dict], registry: EvidenceRegistry | None = None):
        self.repos = repos_by_name
        self.registry = registry if registry is not None else EvidenceRegistry()
        self._profiles: dict[str, dict] = {}

    def _repo(self, name: str) -> dict:
        try:
            return self.repos[name]
        except KeyError:
            raise ValueError(f"unknown repo {name!r}; valid repos: {sorted(self.repos)}") from None

    def _scope(self, name: str | None) -> dict[str, dict]:
        return {name: self._repo(name)} if name else self.repos

    def _f(self, repo: str, path: str) -> str:
        """`[F#] path` for a file the model is being shown."""
        return f"[{self.registry.ref(repo, path)}] {path}"

    def call(self, tool: str, args: dict) -> str:
        fn = getattr(self, f"_t_{tool}", None)
        if fn is None:
            raise ValueError(f"unknown tool {tool!r}")
        return _cap(fn(**args))

    def _t_list_repos(self) -> str:
        for name, repo in self.repos.items():
            if name not in self._profiles:
                self._profiles[name] = build_profile(name, repo)
        return json.dumps(list(self._profiles.values()), indent=1)

    def _t_search_code(self, query: str, repo: str | None = None, mode: str = "fts") -> str:
        iq_mode = "trigram" if mode == "literal" else "fts"
        blocks = []
        for name, r in self._scope(repo).items():
            hits = iq.search_text(r["id"], query, mode=iq_mode, limit=SEARCH_HITS_PER_REPO)
            if not hits:
                continue
            lines = [f"## {name} ({len(hits)} hit{'s' if len(hits) != 1 else ''})"]
            for h in hits:
                snippet = " | ".join(ln.strip() for ln in h.snippet.splitlines())[:200]
                lines.append(f"- {self._f(name, h.path)}: {snippet}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) or "no hits"

    def _t_find_symbol(self, name: str, repo: str | None = None) -> str:
        lines = []
        for rname, r in self._scope(repo).items():
            for h in iq.find_symbols(r["id"], name, fuzzy=True, limit=10):
                lines.append(f"{rname}: {h.qualified_name} ({h.kind}) {self._f(rname, h.path)}:{h.start_line}  {h.signature}")
        return "\n".join(lines) or "no symbols"

    def _t_find_tables(self, name: str | None = None, repo: str | None = None) -> str:
        lines = []
        for rname, r in self._scope(repo).items():
            for h in iq.find_tables(r["id"], name):
                lines.append(f"{rname}: {h.name} ({h.kind}) {self._f(rname, h.path)}:{h.start_line}  {h.signature[:120]}")
        return "\n".join(lines) or "no tables"

    def _t_list_files(self, repo: str, prefix: str = "") -> str:
        paths = [p for p in iq.list_paths(self._repo(repo)["id"]) if p.startswith(prefix)]
        shown = paths[:150]
        extra = f"\n[+{len(paths) - len(shown)} more]" if len(paths) > len(shown) else ""
        return "\n".join(self._f(repo, p) for p in shown) + extra if shown else "no files"

    def _t_file_outline(self, repo: str, path: str) -> str:
        syms = iq.get_symbols_in_file(self._repo(repo)["id"], path)
        if not syms:
            return "no symbols (file missing or has none)"
        body = "\n".join(f"{s.start_line}: {s.kind} {s.qualified_name}  {s.signature}" for s in syms)
        return f"{self._f(repo, path)}\n{body}"

    def _t_read_file(self, repo: str, path: str, start_line: int = 1, max_lines: int = 120) -> str:
        content = iq.get_file(self._repo(repo)["id"], path)
        if content is None:
            raise ValueError(f"{path!r} is not an indexed file in {repo!r}")
        lines = content.splitlines()
        start = max(1, start_line)
        window = lines[start - 1 : start - 1 + max(1, min(max_lines, 300))]
        body = "\n".join(f"{start + i}: {ln}" for i, ln in enumerate(window))
        return f"{self._f(repo, path)}\n{body}"

    def _t_imports_of(self, repo: str, path: str) -> str:
        rows = iq.imports_of_file(self._repo(repo)["id"], path)
        return "\n".join(
            f"{r['raw_specifier']} -> "
            f"{self._f(repo, r['imported_path']) if r['imported_path'] else 'external'}  {r['names']}"
            for r in rows
        ) or "no imports"

    def _t_importers_of(self, repo: str, path: str) -> str:
        return "\n".join(self._f(repo, p) for p in iq.importers_of_file(self._repo(repo)["id"], path)) or "no importers"


# -------------------------------------------------------------- validation


def _citation_problem(repo: str, file_id: str, registry: EvidenceRegistry) -> str | None:
    found = registry.get(file_id)
    if found is None:
        return f"file {file_id!r} was never shown by a tool; cite only numbers like F12 from tool results"
    if found[0] != repo:
        return f"file {file_id} is in repo {found[0]!r}, not {repo!r}; cite files from the repo you are describing"
    return None


def validate_answer(answer: AgentAnswer, repos_by_name: dict[str, dict],
                    registry: EvidenceRegistry) -> list[str]:
    """Closed-set check. Empty list = valid. Every message is addressed to the
    model (it is fed back verbatim on the repair call)."""
    errors: list[str] = []
    seen: set[str] = set()
    for sel in answer.repos:
        if sel.repo not in repos_by_name:
            errors.append(f"repo {sel.repo!r} is not in this project; valid: {sorted(repos_by_name)}")
            continue
        if sel.repo in seen:
            errors.append(f"repo {sel.repo!r} is listed more than once")
        seen.add(sel.repo)
        for ev in sel.evidence:
            if (problem := _citation_problem(sel.repo, ev.file, registry)):
                errors.append(problem)
    if not any(s.role == "primary" for s in answer.repos):
        errors.append("no primary repo selected; at least one repo must be primary")

    # Every repo must be decided explicitly: selected, or ruled out with a reason.
    ruled: set[str] = set()
    for r in answer.ruled_out:
        if r.repo not in repos_by_name:
            errors.append(f"ruled_out names {r.repo!r}, which is not in this project; valid: {sorted(repos_by_name)}")
        elif r.repo in seen:
            errors.append(f"repo {r.repo!r} is both selected and in ruled_out")
        elif r.repo in ruled:
            errors.append(f"repo {r.repo!r} is listed more than once in ruled_out")
        ruled.add(r.repo)
    undecided = sorted(set(repos_by_name) - seen - ruled)
    if undecided:
        errors.append(
            "these repos were not decided: " + ", ".join(undecided)
            + ". Every repo must be either in `repos` or in `ruled_out` with a reason "
            "(search for it first if you have not looked at it)."
        )
    return errors


def prune_invalid_citations(answer: AgentAnswer, repos_by_name: dict[str, dict],
                            registry: EvidenceRegistry) -> tuple[AgentAnswer, list[dict]]:
    """Last resort after a failed repair: drop citations that don't check out.
    A repo stays only if it keeps at least one valid citation. Returns
    (answer, dropped)."""
    pruned = answer.model_copy(deep=True)
    dropped: list[dict] = []
    keep = []
    for sel in pruned.repos:
        good = [ev for ev in sel.evidence if _citation_problem(sel.repo, ev.file, registry) is None]
        dropped += [{"repo": sel.repo, "file": ev.file} for ev in sel.evidence if ev not in good]
        if good:
            sel.evidence = good
            keep.append(sel)
        elif sel.repo in repos_by_name:
            dropped.append({"repo": sel.repo, "file": None, "reason": "no valid evidence; repo removed"})
    pruned.repos = keep
    # a repo whose evidence was all invalid is now undecided: rule it out so the
    # answer stays complete (the reason says why it is not listed)
    for d in dropped:
        if d.get("file") is None and d["repo"] in repos_by_name:
            pruned.ruled_out.append(AgentRuledOut(
                repo=d["repo"], reason="dropped: its cited evidence could not be verified"))
    return pruned, dropped


def resolve_answer(answer: AgentAnswer, repos_by_name: dict[str, dict],
                   registry: EvidenceRegistry) -> list[ResolvedRepo]:
    """Turn the validated answer into the public result: the real path for
    every cited number is written here, by code. Primary first."""
    resolved = []
    for sel in sorted(answer.repos, key=lambda s: s.role != "primary"):
        evidence = [RepoEvidence(path=registry.get(ev.file)[1], note=ev.note) for ev in sel.evidence]
        resolved.append(ResolvedRepo(
            repo=sel.repo, role=sel.role, reason=sel.reason, evidence=evidence,
            repo_id=repos_by_name[sel.repo]["id"],
        ))
    return resolved


def resolve_ruled_out(answer: AgentAnswer, repos_by_name: dict[str, dict]) -> list[RuledOutRepo]:
    return [
        RuledOutRepo(repo=r.repo, repo_id=repos_by_name[r.repo]["id"], reason=r.reason)
        for r in answer.ruled_out if r.repo in repos_by_name
    ]


# ------------------------------------------------------------------ router


def _user_prompt(request_text: str, attachments: list[str] | None) -> str:
    parts = [f"## Request\n{request_text}"]
    for i, att in enumerate(attachments or [], 1):
        parts.append(f"## Attachment {i}\n{att[:3000]}")
    return "\n\n".join(parts)


def summarize_result(result: str) -> str:
    """One short line describing a tool result, for --trace."""
    import re

    if result.startswith("error:"):
        return result[:160]
    hits = re.findall(r"^## (\S+) \((\d+) hit", result, re.M)
    if hits:
        return "hits in " + ", ".join(f"{repo}({n})" for repo, n in hits)
    first = result.strip().splitlines()[0] if result.strip() else "(empty)"
    n = len(result.strip().splitlines())
    return first[:100] if n == 1 else f"{n} lines, first: {first[:80]}"


def _traced(tools: "RouterTools", trace: Callable[[str], None]) -> Callable[[str, dict], str]:
    """Wrap tools.call so every lookup the agent makes is reported as it
    happens, with a one-line summary of what came back."""
    count = 0

    def call(name: str, args: dict) -> str:
        nonlocal count
        count += 1
        shown = json.dumps(args, ensure_ascii=False)
        try:
            result = tools.call(name, args)
        except Exception as exc:  # noqa: BLE001 - re-raised below, the loop reports it to the model
            trace(f"  {count:>2}. {name} {shown}\n        -> ERROR: {exc}"[:400])
            raise
        trace(f"  {count:>2}. {name} {shown}\n        -> {summarize_result(result)}")
        return result

    return call


def _parse_answer(text: str) -> AgentAnswer | str:
    """Parsed answer, or the error string to feed back."""
    from src.llm import _strip_fences

    try:
        return AgentAnswer.model_validate(json.loads(_strip_fences(text)))
    except Exception as exc:  # noqa: BLE001 - json or pydantic; both go back to the model
        return f"your answer was not valid JSON for the schema: {exc}"


def select_repos(
    project_id: int,
    request_text: str,
    attachments: list[str] | None = None,
    *,
    gateway: LLMGateway | None = None,
    run_id: int | None = None,
    budget: TokenBudget | None = None,
    trace: Callable[[str], None] | None = None,
) -> RepoSelection:
    """`trace`, if given, is called with one block of text per lookup the agent
    makes (the tool, its arguments, and what came back), as it happens."""
    repos = iq.get_project_repos(project_id)
    if not repos:
        raise RepoSelectionError(f"project {project_id} has no repos")
    by_name = repo_names(repos)

    settings = get_settings()
    gateway = gateway or LLMGateway()
    schema_json = json.dumps(AgentAnswer.model_json_schema())
    budget_note = (
        f"## Budget\nYou have at most {settings.router_max_turns} model turns, and each tool-using "
        f"turn counts. Plan to give your final answer by turn {max(1, settings.router_max_turns - 3)}; "
        "at the limit the tools are switched off and you must answer from what you have."
    )
    system = f"{load_prompt('repo_router')}\n\n{budget_note}\n\nSchema:\n{schema_json}"
    user = _user_prompt(request_text, attachments)
    registry = EvidenceRegistry()
    tools = RouterTools(by_name, registry)

    loop = gateway.run_tool_loop(
        tier=settings.router_tier, system=system, user=user,
        tools=TOOLS, handler=_traced(tools, trace) if trace else tools.call, max_turns=settings.router_max_turns,
        purpose="repo_router", run_id=run_id, budget=budget,
    )

    def check(candidate: AgentAnswer | str) -> tuple[AgentAnswer | None, list[str]]:
        if isinstance(candidate, str):
            return None, [candidate]
        return candidate, validate_answer(candidate, by_name, registry)

    answer, errors = check(_parse_answer(loop.text))
    repaired = bool(errors)
    dropped: list[dict] = []
    if errors:
        # One repair. The model can't look things up here, so it is handed the
        # complete list of citable files along with every problem verbatim.
        repair_user = (
            f"{user}\n\n## Your previous answer\n{loop.text}\n\n## Problems\n"
            + "\n".join(f"- {e}" for e in errors)
            + f"\n\n## Files you may cite (number, repo, path)\n{registry.listing()}"
            + "\n\nReturn corrected JSON. Cite files only by these numbers."
        )
        reply = gateway.complete_json(
            tier=settings.router_tier, system=system, user=repair_user,
            schema=AgentAnswer, purpose="repo_router_repair", run_id=run_id, budget=budget,
        )
        answer, errors = check(reply)
        if errors:
            # Still citing something that doesn't check out: keep what is
            # verifiable rather than fail the request over one citation.
            pruned, dropped = prune_invalid_citations(reply, by_name, registry)
            answer, errors = check(pruned)
            if errors:
                raise RepoSelectionError("repo selection invalid after repair: " + "; ".join(errors))

    return RepoSelection(
        project_id=project_id,
        repos=resolve_answer(answer, by_name, registry),
        ruled_out=resolve_ruled_out(answer, by_name),
        stats={
            "turns": loop.turns,
            "tool_calls": len(loop.tool_calls),
            "exhausted": loop.exhausted,
            "repaired": repaired,
            "files_numbered": len(registry),
            "dropped_evidence": dropped,
        },
    )
