"""Per-run debug transcript: one markdown file per run showing the input and
output of every step, deterministic or LLM. Observability only — `log()`
never raises, returns nothing, and never mutates what it's handed. See
docs/... none yet; see the module it's called from for what each step id
means.

Disable entirely with `DEBUG_LOG_DIR=""`.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from pathlib import Path

from src.config import get_settings

_LOCK = threading.Lock()

# gateway call `purpose` -> transcript step id. Unknown purposes fall back to
# [?] rather than raising — this dict is display-only, never load-bearing.
PURPOSE_STEP = {
    "entity_extraction": "[A1]",
    "rerank": "[A6]",
    "spec": "[B1]",
    "spec_repair": "[B1r]",
    "spec_answers": "[G1]",
    "spec_chat": "[G1c]",
    "plan": "[B3]",
    "plan_repair": "[B3r]",
    "plan_chat": "[G2]",
}


def _render(value) -> str:
    if isinstance(value, str):
        return f"```\n{value}\n```"
    return f"```json\n{json.dumps(value, indent=2, default=str)}\n```"


def log(run_id, step: str, *, input=None, output=None, **extra) -> None:
    """Append one entry to `<debug_log_dir>/run-<run_id>.md`. `step` is a
    human label like "[A3] context:retrieve". `input`/`output`/any extra
    kwarg become their own labelled section, rendered as plain text (str) or
    pretty JSON (everything else, via `default=str`).
    """
    settings = get_settings()
    log_dir = settings.debug_log_dir
    if not log_dir or run_id is None:
        return
    try:
        path = Path(log_dir) / f"run-{run_id}.md"
        sections = []
        if input is not None:
            sections.append(("INPUT", input))
        if output is not None:
            sections.append(("OUTPUT", output))
        sections.extend(extra.items())

        lines = [f"## {step} · {dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}\n"]
        for name, value in sections:
            lines.append(f"### {name.upper()}\n{_render(value)}\n")
        entry = "\n".join(lines)

        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if is_new:
                    f.write(f"# Run {run_id}\n\n")
                f.write(entry + "\n")
    except Exception as exc:  # noqa: BLE001 - a transcript must never fail a run
        print(f"[debug_log] failed to write: {exc}", file=sys.stderr)
