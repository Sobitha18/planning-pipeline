# Golden eval harness

Replay-based regression testing for Stage B (spec + plan generation):
capture a real run's output as a golden reference, then replay the SAME
stored context pack through the current prompt/model and score the fresh
output against it.

**Rule: every prompt or model change (`src/prompts/*.md`, `LLM_STRONG_MODEL`,
`src/planning.py`'s generation/validation logic) requires a green
`python -m evals.run_evals` before merge.** It replays every case in
`evals/cases/` and exits non-zero if any hard gate fails — wire it into CI
the same way.

## Running it

```bash
export DATABASE_URL=postgresql+psycopg://<user>@localhost:5432/ppl_smoke   # or wherever the case repos are indexed
export ANTHROPIC_API_KEY=...        # Stage B is a real LLM call
python -m evals.run_evals
```

Requires the repo each case names (`repo_root_path`) to still be indexed in
the DB `DATABASE_URL` points at — Stage B validation checks file paths
against the live index, not a snapshot.

## What's scored

Hard gates (any failure -> case fails, process exit code 1):
- the fresh validation report passes
- fresh `touchable_files` ⊆ (golden `touchable_files` ∪ golden context-pack file paths)
- criteria count within ±1 of golden
- task count within ±2 of golden

Soft metrics (printed, never fail a case): Jaccard of `touchable_files`,
Jaccard of the union of all tasks' `files`, a token-set text-similarity
ratio (`difflib`, no embeddings) over the criteria G/W/T text, and the
open-question count delta.

Only Stage B is replayed — the stored `context_pack` is reused as-is rather
than re-running retrieval, since prompt changes (the thing this harness
guards) land in Stage B far more often than in retrieval.

## Adding a case

1. Run the pipeline for real (`POST /v1/runs` ... approve/reject as normal)
   against a repo that's indexed in whatever DB you're capturing from.
2. Capture it:
   ```bash
   python -m evals.capture <run_id> --name <case_name>
   ```
   This writes `evals/cases/<case_name>.json` (request, repo root_path +
   indexed_sha, context_pack, spec_plan, validation report) — the fields
   `evals.run_evals` needs, and nothing else.
3. Run `python -m evals.run_evals` once to confirm the new case is green
   against itself, then commit the JSON file.

Aim for a spread: at least one bug report, one clearly-specified feature,
and one intentionally vague feature (to exercise `open_questions`) per repo
you care about.
