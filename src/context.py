"""Stage A — Context Engine: request + indexed repo -> Context Pack.

entities -> retrieve (3 channels, union) -> expand (symbol graph) -> rerank
-> budget-pack. Two cheap LLM calls (extraction, rerank); everything else is
deterministic. Reads the index ONLY through src.index_query.

Out of scope (task-prompts/05): vector channel, arch_summary, anchor/modifier
machinery, multi-round retrieval, caching.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from src import index_query as iq
from src.llm import LLMGateway, load_prompt
from src.models import (
    ContextPack,
    ExtractedEntities,
    PackedFile,
    RerankResponse,
    StackFrame,
)

TOKEN_BUDGET = 25_000
MAX_SEEDS = 15
MAX_FILES = 25
COMMON_NAME_LIMIT = 10   # a bare name matching more than this is a common word
MIN_RESOLVED_NEIGHBOURS = 5
RERANK_SYMBOLS_PER_FILE = 12


def est_tokens(text: str) -> int:
    """Same estimator as the LLM gateway: ~3.5 chars/token."""
    return int(len(text) / 3.5)


# --------------------------------------------------------------- stack frames

_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
# `at fn (src/x.ts:12:5)`, `at src/x.ts:12:5`, webpack/next variants.
_JS_FRAME = re.compile(r"([^\s()]+\.(?:[cm]?[jt]sx?)):(\d+)(?::\d+)?")
_JS_PREFIXES = ("webpack-internal:///", "webpack://", "file://", "rsc://React/Server/")


def _normalize_frame_path(path: str) -> str:
    path = path.strip()
    for prefix in _JS_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
    # webpack layers a build-name segment in front: `_N_E/./src/x.ts`
    path = re.sub(r"^[^/]*_N_E/", "", path)
    while path.startswith("./"):
        path = path[2:]
    return path


def parse_stack_frames(attachments: list[str]) -> list[StackFrame]:
    """Deterministic (regex) stack-frame parsing — never the LLM's job.

    Handles Python tracebacks and JS/TS stacks; drops node_modules frames.
    Paths are normalized but NOT yet checked against the index.
    """
    frames: list[StackFrame] = []
    seen: set[tuple[str, int | None]] = set()
    for text in attachments:
        for line in text.splitlines():
            matches = [(m.group(1), m.group(2)) for m in _PY_FRAME.finditer(line)]
            if not matches and re.search(r"(^|\s)at\s", line):
                matches = [(m.group(1), m.group(2)) for m in _JS_FRAME.finditer(line)]
            for raw, lineno in matches:
                path = _normalize_frame_path(raw)
                if not path or "node_modules" in path:
                    continue
                key = (path, int(lineno))
                if key in seen:
                    continue
                seen.add(key)
                frames.append(StackFrame(path=path, line=int(lineno)))
    return frames


def resolve_indexed_path(repo_id, path: str, cache: dict | None = None) -> str | None:
    """Map an absolute/odd stack-frame or mentioned path onto an indexed
    repo-relative path by trying successively shorter suffixes.

    ponytail: O(depth) get_file probes per path, and get_file pulls whole file
    content. Fine for the handful of frames a request carries; add a
    path-only lookup to index_query if a request ever arrives with hundreds.
    """
    if cache is not None and path in cache:
        return cache[path]
    parts = [p for p in path.split("/") if p not in ("", ".")]
    resolved = None
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if iq.get_file(repo_id, candidate) is not None:
            resolved = candidate
            break
    if cache is not None:
        cache[path] = resolved
    return resolved


# ----------------------------------------------------------------- retrieval


@dataclass
class _Candidate:
    path: str
    keys: set = field(default_factory=set)      # distinct (channel, entity) hits
    score: float = 0.0
    symbols: dict = field(default_factory=dict)  # symbol id -> qualified name


def _channel_weights(request_type: str) -> dict[str, float]:
    """Bugs live in exact identifiers and literal error text; features live in
    prose-shaped matches over the request and its domain words."""
    bug = request_type == "bug"
    return {
        "symbol": 1.5 if bug else 1.0,
        "trigram": 1.5 if bug else 1.0,
        "fts": 1.0 if bug else 1.5,
        "domain": 1.0 if bug else 1.5,
        "mention": 1.5,
    }


def _add_hit(cands: dict, path: str, key: tuple, score: float, weight: float) -> _Candidate:
    cand = cands.setdefault(path, _Candidate(path=path))
    cand.keys.add(key)
    cand.score += score * weight
    return cand


def _add_file_hits(cands: dict, hits, key: tuple, weight: float) -> int:
    """Scores from different channels live on different scales (ts_rank vs
    trigram similarity), so normalize each result list by its own top hit."""
    if not hits:
        return 0
    top = max(h.score for h in hits) or 1.0
    for hit in hits:
        _add_hit(cands, hit.path, key, hit.score / top, weight)
    return len(hits)


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "when", "should", "would",
    "does", "not", "are", "was", "were", "but", "you", "our", "can", "has", "have",
    "had", "its", "into", "some", "any", "all", "add", "new", "get", "use", "used",
    "using", "need", "needs", "want", "make", "sure", "please", "issue", "bug",
    "feature", "request", "error", "fix", "fixed", "code", "app", "user", "users",
}


def fts_query(text: str, max_terms: int = 12) -> str:
    """websearch_to_tsquery ANDs every term, and the 'simple' search config
    keeps stopwords as real lexemes — so a whole-sentence query matches
    nothing. Reduce to distinctive content words joined with OR.

    ponytail: fixed stopword list, no IDF weighting. Swap in a real
    rarity ranking if precision on long requests becomes the complaint.
    """
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())
    terms = [w for w in dict.fromkeys(words) if w not in _STOPWORDS][:max_terms]
    return " or ".join(terms)


def _domain_match(path: str, domains: list[str]) -> bool:
    lowered = path.lower()
    return any(d.lower() in lowered for d in domains if d)


def retrieve(repo_id, request_text: str, entities: ExtractedEntities,
             frame_paths: list[str]) -> tuple[dict, dict]:
    """Three channels (symbol / text / domain), unioned into per-file
    candidates carrying which distinct entities hit them."""
    weights = _channel_weights(entities.request_type)
    cands: dict[str, _Candidate] = {}
    stats = {"symbol": 0, "trigram": 0, "fts": 0, "domain": 0, "mention": 0,
             "dropped_common_names": []}
    path_cache: dict = {}

    # -- symbol channel
    for name in entities.symbols:
        if not name.strip():
            continue
        hits = iq.find_symbols(repo_id, name)
        base = 1.0
        if not hits:
            hits = iq.find_symbols(repo_id, name, fuzzy=True)
            base = 0.5
        if "." not in name and len(hits) > COMMON_NAME_LIMIT:
            narrowed = [h for h in hits if _domain_match(h.path, entities.domains)]
            if not narrowed:
                stats["dropped_common_names"].append(name)
                continue
            hits = narrowed
        for hit in hits:
            cand = _add_hit(cands, hit.path, ("symbol", name), base, weights["symbol"])
            cand.symbols[hit.id] = hit.qualified_name
        stats["symbol"] += len(hits)

    # -- text channel: literal error strings, then the request itself
    for error in entities.error_strings:
        if not error.strip():
            continue
        stats["trigram"] += _add_file_hits(
            cands, iq.search_text(repo_id, error, mode="trigram"),
            ("trigram", error), weights["trigram"],
        )

    request_query = fts_query(request_text)
    if request_query:
        stats["fts"] += _add_file_hits(
            cands, iq.search_text(repo_id, request_query, mode="fts"),
            ("fts", "request"), weights["fts"],
        )

    # -- domain channel
    domain_query = fts_query(" ".join(d for d in entities.domains if d.strip()))
    if domain_query:
        stats["domain"] += _add_file_hits(
            cands, iq.search_text(repo_id, domain_query, mode="fts"),
            ("domain", "domains"), weights["domain"],
        )

    # -- files the request named outright
    for mentioned in entities.files_mentioned:
        path = resolve_indexed_path(repo_id, mentioned, path_cache)
        if path:
            _add_hit(cands, path, ("mention", mentioned), 1.0, weights["mention"])
            stats["mention"] += 1

    # stack-frame files are seeds by construction, but still deserve a score
    for path in frame_paths:
        _add_hit(cands, path, ("stack", path), 1.0, 1.0)

    return cands, stats


def rank_seeds(cands: dict, frame_paths: list[str]) -> list[str]:
    """Intersection first: a file hit by three distinct entities beats a file
    hit by one, whatever its raw score. Score only breaks ties."""
    ranked = sorted(cands.values(), key=lambda c: (len(c.keys), c.score), reverse=True)
    seeds = list(dict.fromkeys(frame_paths))
    for cand in ranked:
        if len(seeds) >= max(MAX_SEEDS, len(frame_paths)):
            break
        if cand.path not in seeds:
            seeds.append(cand.path)
    return seeds


# ----------------------------------------------------------------- expansion


def expand(repo_id, seeds: list[str], cands: dict, frame_paths: set) -> tuple[list[str], dict, dict]:
    """1 hop over the symbol graph from the seed symbols, plus their tests."""
    seed_symbols: dict[int, str] = {}
    for path in seeds:
        cand = cands.get(path)
        if cand and cand.symbols:
            seed_symbols.update(cand.symbols)
        elif path in frame_paths:
            syms = iq.get_symbols_in_file(repo_id, path)
            exported = [s for s in syms if s.exported] or syms
            seed_symbols.update({s.id: s.qualified_name for s in exported})

    neighbours = []
    if seed_symbols:
        neighbours = iq.neighbors(
            repo_id, list(seed_symbols), direction="both", min_confidence="resolved",
        )
        if len(neighbours) < MIN_RESOLVED_NEIGHBOURS:
            neighbours = iq.neighbors(
                repo_id, list(seed_symbols), direction="both", min_confidence="heuristic",
            )

    extra: dict[str, int] = {}
    for hit in neighbours:
        if hit.path not in seeds:
            extra[hit.path] = extra.get(hit.path, 0) + 1

    names = sorted({qn.split(".")[-1] for qn in seed_symbols.values()})
    test_hits = iq.tests_of(repo_id, names) if names else []
    for hit in test_hits:
        if hit.path not in seeds:
            extra.setdefault(hit.path, 0)
            extra[hit.path] += 2  # a seed's test file is worth more than a stray callee

    ordered_extra = sorted(extra, key=lambda p: (-extra[p], p))
    files = seeds[:MAX_FILES] + ordered_extra
    files = files[:MAX_FILES]
    stats = {
        "seed_symbols": len(seed_symbols),
        "neighbour_symbols": len(neighbours),
        "test_files": len(test_hits),
        "expansion_candidates": len(extra),
        "files_after_expansion": len(files),
    }
    return files, seed_symbols, stats


# -------------------------------------------------------------------- rerank


def _symbol_digest(repo_id, path: str) -> str:
    lines = []
    for sym in iq.get_symbols_in_file(repo_id, path)[:RERANK_SYMBOLS_PER_FILE]:
        doc = (sym.docstring or "").strip().splitlines()
        head = f" — {doc[0][:120]}" if doc else ""
        lines.append(f"- {sym.signature.strip()}{head}")
    return "\n".join(lines) or "- (no top-level symbols indexed)"


def rerank(gateway, repo_id, request_text: str, files: list[str],
           protected: set) -> tuple[list[tuple[str, str, str]], dict]:
    """One cheap LLM call labels every candidate. Seeds and stack-frame files
    may be down-labeled but never dropped — the LLM sees signatures only, so
    it is not allowed to overrule deterministic evidence."""
    blocks = "\n\n".join(f"### {p}\n{_symbol_digest(repo_id, p)}" for p in files)
    user = f"REQUEST:\n{request_text}\n\nCANDIDATE FILES:\n\n{blocks}"
    response = gateway.complete_json(
        tier="cheap", system=load_prompt("rerank"), user=user,
        schema=RerankResponse, purpose="rerank", max_tokens=4096,
    )
    labels = {item.path: (item.label, item.reason) for item in response.files}

    kept: list[tuple[str, str, str]] = []
    dropped = 0
    for path in files:
        label, reason = labels.get(path, ("peripheral", "not labeled by rerank"))
        if label == "irrelevant":
            if path not in protected:
                dropped += 1
                continue
            label, reason = "peripheral", f"retained as seed (rerank said irrelevant: {reason})"
        kept.append((path, label, reason))
    return kept, {"reranked": len(files), "dropped_by_rerank": dropped}


# ------------------------------------------------------------------- packing


def _tier_content(repo_id, path: str, tier: int, reason: str) -> str:
    if tier == 1:
        return iq.get_file(repo_id, path) or ""
    if tier == 2:
        return _symbol_digest(repo_id, path)
    return reason


def pack(repo_id, labeled: list[tuple[str, str, str]], frame_paths: set,
         budget: int = TOKEN_BUDGET) -> tuple[list[PackedFile], dict]:
    """Greedy by rank with tiered fidelity. Over budget: demote the largest
    non-stack-frame Tier 1 to Tier 2, then shed Tier 3, then demote Tier 2.
    Files are never truncated mid-content — only demoted or dropped whole.
    """
    packed: list[PackedFile] = []
    for path, label, reason in labeled:
        tier = 1 if (path in frame_paths or label == "critical") else 2 if label == "relevant" else 3
        packed.append(PackedFile(
            path=path, tier=tier, label=label, reason=reason,
            content=_tier_content(repo_id, path, tier, reason),
        ))

    def total() -> int:
        return sum(est_tokens(f.content) for f in packed)

    demotions = dropped = 0
    while total() > budget:
        demotable = [f for f in packed if f.tier == 1 and f.path not in frame_paths]
        if demotable:
            victim = max(demotable, key=lambda f: len(f.content))
            victim.tier = 2
            victim.content = _tier_content(repo_id, victim.path, 2, victim.reason)
            demotions += 1
            continue
        tier3 = [f for f in packed if f.tier == 3]
        if tier3:
            packed.remove(tier3[-1])  # lowest-ranked peripheral goes first
            dropped += 1
            continue
        tier2 = [f for f in packed if f.tier == 2]
        if tier2:
            victim = tier2[-1]
            victim.tier = 3
            victim.content = victim.reason
            demotions += 1
            continue
        break  # only stack-frame files left: they are the request, keep them

    return packed, {"demotions": demotions, "dropped_for_budget": dropped}


def _recent_commits(root_path: str | None, paths: list[str], limit: int = 10) -> list[dict]:
    """Last N commits touching the Tier-1 files. Not a git repo / git missing
    / detached weirdness -> [] (never fatal)."""
    if not root_path or not paths:
        return []
    try:
        out = subprocess.run(
            ["git", "log", f"-n{limit}", "--pretty=format:%x1e%H%x1f%s", "--name-only",
             "--", *paths],
            cwd=root_path, capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    commits = []
    for record in out.split("\x1e"):
        if not record.strip():
            continue
        header, _, files = record.partition("\n")
        sha, _, message = header.partition("\x1f")
        commits.append({
            "sha": sha.strip()[:12],
            "message": message.strip(),
            "files": [f for f in files.splitlines() if f.strip()],
        })
    return commits


# ------------------------------------------------------------------ pipeline


def build_context(repo_id, request_text: str, attachments: list[str] | None = None,
                  *, gateway: LLMGateway | None = None) -> ContextPack:
    """Stage A end to end. `gateway` is injectable so tests never hit the API."""
    attachments = attachments or []
    gateway = gateway or LLMGateway()
    repo = iq.get_repo(repo_id) or {}

    entities = gateway.complete_json(
        tier="cheap", system=load_prompt("entity_extraction"), user=request_text,
        schema=ExtractedEntities, purpose="entity_extraction", max_tokens=1024,
    )

    cache: dict = {}
    frames = parse_stack_frames(attachments)
    frame_paths: list[str] = []
    for frame in frames:
        path = resolve_indexed_path(repo_id, frame.path, cache)
        if path and path not in frame_paths:
            frame_paths.append(path)

    cands, retrieval_stats = retrieve(repo_id, request_text, entities, frame_paths)
    seeds = rank_seeds(cands, frame_paths)
    frame_set = set(frame_paths)
    files, seed_symbols, expand_stats = expand(repo_id, seeds, cands, frame_set)

    protected = set(seeds) | frame_set
    labeled, rerank_stats = rerank(gateway, repo_id, request_text, files, protected)
    packed, pack_stats = pack(repo_id, labeled, frame_set)

    tier1 = [f.path for f in packed if f.tier == 1]
    commits = _recent_commits(repo.get("root_path"), tier1)
    token_count = sum(est_tokens(f.content) for f in packed)

    return ContextPack(
        repo_id=repo_id,
        sha=repo.get("indexed_sha"),
        request_type=entities.request_type,
        files=packed,
        seed_symbols=sorted(seed_symbols.values()),
        recent_commits=commits,
        token_count=token_count,
        stats={
            "entities": entities.model_dump(),
            "stack_frames_parsed": len(frames),
            "stack_frames_in_index": len(frame_paths),
            "seeds_per_channel": {k: v for k, v in retrieval_stats.items()
                                  if k != "dropped_common_names"},
            "dropped_common_names": retrieval_stats["dropped_common_names"],
            "candidate_files": len(cands),
            "seed_files": len(seeds),
            **expand_stats,
            **rerank_stats,
            **pack_stats,
            "tiers": {t: sum(1 for f in packed if f.tier == t) for t in (1, 2, 3)},
            "token_count": token_count,
        },
    )
