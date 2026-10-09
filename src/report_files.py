"""Files named in a request: stack-trace frames and paths mentioned in the text,
matched against each repo's index.

Bug reports usually say where the problem showed up (`File "app/x.py", line 42`,
`at src/screens/Cart.tsx:10:5`, "see orders/routes.py"). Finding those files is
deterministic string work, so it is done here, in code, before the agent starts:
the agent is then handed the files that really exist in the index (with their
citation numbers) instead of having to notice and search for them.

Only existing indexed files are ever returned, so a path that is merely
mentioned, or is absolute on someone else's machine, can't invent evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.context import parse_stack_frames

MAX_MATCHES_PER_MENTION_PER_REPO = 3
MAX_LOCATED = 30

# a path-looking token ending in a source extension, optional :line
_MENTION = re.compile(
    r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\.(?:py|tsx?|jsx?|mjs|cjs|prisma|sql))(?::(\d+))?(?![\w/])"
)


@dataclass(frozen=True)
class Located:
    repo: str
    path: str
    line: int | None = None


def extract_mentions(texts: list[str]) -> list[tuple[str, int | None]]:
    """Path-like strings in `texts`, with a line number when one is given.
    Stack-trace frames (Python and JS/TS) come first, then bare mentions."""
    mentions: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def add(path: str, line: int | None) -> None:
        path = path.strip().strip("`'\"()[],;")
        while path.startswith("./"):
            path = path[2:]
        if path and "node_modules" not in path and path not in seen:
            seen.add(path)
            mentions.append((path, line))

    for frame in parse_stack_frames(texts):
        add(frame.path, frame.line)
    for text in texts:
        for m in _MENTION.finditer(text):
            add(m.group(1), int(m.group(2)) if m.group(2) else None)
    return mentions


def _matches(mention: str, indexed: str) -> bool:
    """True if `mention` names `indexed`, allowing either to carry extra leading
    directories (an absolute path in a trace, or a shortened one in prose).
    A bare file name matches any file with that name."""
    if mention == indexed:
        return True
    if "/" not in mention:
        return indexed.rsplit("/", 1)[-1] == mention
    return mention.endswith("/" + indexed) or indexed.endswith("/" + mention)


def locate(mentions: list[tuple[str, int | None]], paths_by_repo: dict[str, set[str]]) -> list[Located]:
    """Indexed files the mentions name, per repo, in mention order. Capped, so a
    vague mention (a bare `index.ts`) cannot flood the prompt."""
    out: list[Located] = []
    seen: set[tuple[str, str]] = set()
    for mention, line in mentions:
        for repo, paths in paths_by_repo.items():
            hits = sorted(p for p in paths if _matches(mention, p))
            for path in hits[:MAX_MATCHES_PER_MENTION_PER_REPO]:
                if (repo, path) not in seen:
                    seen.add((repo, path))
                    out.append(Located(repo, path, line))
        if len(out) >= MAX_LOCATED:
            break
    return out[:MAX_LOCATED]
