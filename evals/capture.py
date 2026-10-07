"""Snapshot a completed, live run into a golden eval case.

    python -m evals.capture <run_id> --name <case_name>

Reads from whatever DATABASE_URL is set in the environment (point it at the
DB the run actually happened in — e.g. the shared `ppl_smoke` scratch DB).
Writes evals/cases/<case_name>.json: {request, repo root_path + indexed_sha,
context_pack, spec_plan, validation} — everything `evals.run_evals` needs to
replay Stage B and score the result against this as ground truth.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from src import runs as runs_mod
from src.db import get_session
from src.models import Repo

CASES_DIR = Path(__file__).parent / "cases"


def capture(run_id: str, name: str, *, cases_dir: Path = CASES_DIR) -> Path:
    session = get_session()
    try:
        run = runs_mod.get_run(session, uuid.UUID(run_id))
        repo = session.get(Repo, run.repo_id)
        if repo is None:
            raise SystemExit(f"run {run_id}: repo {run.repo_id} no longer exists")

        context_pack = runs_mod.latest_artifact(session, run.id, "context_pack")
        spec_plan = runs_mod.latest_artifact(session, run.id, "spec_plan")
        validation = runs_mod.latest_artifact(session, run.id, "validation")
        missing = [k for k, v in (("context_pack", context_pack), ("spec_plan", spec_plan),
                                   ("validation", validation)) if v is None]
        if missing:
            raise SystemExit(f"run {run_id}: missing artifact(s) {missing} — not a completed run")

        case = {
            "name": name,
            "run_id": str(run.id),
            "repo_root_path": repo.root_path,
            "indexed_sha": context_pack.body.get("sha") or repo.indexed_sha,
            "request": {
                "type": run.request_type,
                "text": run.request_text,
                "attachments": run.attachments,
            },
            "context_pack": context_pack.body,
            "spec_plan": spec_plan.body,
            "validation": validation.body,
        }
    finally:
        session.close()

    cases_dir.mkdir(parents=True, exist_ok=True)
    out_path = cases_dir / f"{name}.json"
    out_path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)

    out_path = capture(args.run_id, args.name)
    print(f"captured {args.run_id} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
