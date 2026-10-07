# Task 10: Executor Agent Tools + Golden Eval Harness

## Context

Done: everything through the approval UI. Two final pieces: (1) tool definitions so Claude-Agent-SDK executor agents can consume the pipeline, (2) a replay-based eval harness so prompt/model changes can be regression-tested against stored runs. These make the system usable-by-agents and safely-changeable.

## Part 1 — Agent tools

Files:
```
agent_tools/planning_tools.py    # tool functions + schemas for claude-agent-sdk
agent_tools/README.md            # how to register them in an agent
tests/test_agent_tools.py
```

Implement as plain Python functions with docstrings + a `TOOLS` list of JSON-schema tool definitions (claude-agent-sdk custom-tool format), each thin-wrapping the HTTP API (httpx, base URL from env `PLANNING_API_URL`):

```
create_planning_run(repo_path, request_type, request_text, attachments=[]) -> {run_id, status}
get_run(run_id) -> snapshot                     # includes validation + escalation
get_approved_plan(run_id) -> plan | error       # errors clearly if not yet approved
wait_for_status(run_id, statuses, timeout_s=300, poll_s=2) -> snapshot   # polling helper
approve_plan(run_id, version, answers=None) -> status
reject_plan(run_id, version, feedback) -> status
get_task_context(run_id, task_id) -> {task, files: {path: content}}
    # NEW convenience endpoint to ADD to the API: returns one task + full current
    # content of its files (from the index) — what an executor needs to start a task
next_ready_tasks(run_id, completed_task_ids: list[str]) -> [task_id]
    # pure client-side: topo logic over the approved plan's edges — which tasks
    # are unblocked given completed set (executor's scheduling primitive)
```

Requirements: every tool returns JSON-serializable dicts; errors returned as {error: {...}} not exceptions (agents handle values better than tracebacks); Idempotency-Key auto-generated in create; tool descriptions written FOR an LLM agent (state when to use, what preconditions).

Tests: mock httpx transport; happy path per tool; error mapping (409 → clean error value); next_ready_tasks unit-tested on a diamond DAG (t1 → t2,t3 → t4): initially [t1], after t1 [t2,t3], after t2 [t3] not t4, after t2+t3 [t4].

## Part 2 — Golden eval harness

Files:
```
evals/capture.py       # snapshot a live run into a golden case
evals/run_evals.py     # replay all cases, score, report
evals/cases/           # stored cases (jsonl or per-case dirs)
tests/test_evals.py
```

**Capture:** `python -m evals.capture <run_id> --name <case_name>` → stores {request, repo identifier + indexed_sha, context_pack, final spec_plan, validation report} as the golden reference. Capture 6 cases now from the smoke runs already performed on:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```
(3 per repo: one bug, one clear feature, one vague feature.)

**Replay:** for each case, re-run the pipeline stages against the SAME stored context pack (context stage replay optional/flagged — Stage B replay is the priority since prompts change most) and score the fresh output vs golden:
- hard gates (fail): validation report must pass; touchable_files ⊆ (golden touchable ∪ golden pack paths); criteria count within ±1 of golden; task count within ±2.
- soft metrics (report, don't fail): Jaccard similarity of touchable_files vs golden; of task-file sets; criteria text similarity (embedding-free: token-set ratio via difflib); question count delta.
- Output: table per case + overall pass/fail; exit code non-zero on any hard-gate failure (CI-usable).

Tests: harness logic unit-tested with two synthetic cases (one passing, one hard-gate-failing); scorer functions (jaccard, ratio) unit-tested.

**Document in evals/README.md:** the rule that every prompt or model change requires `python -m evals.run_evals` green before merge, and how to add a case.

## Definition of done

- All unit tests green.
- Live: a scripted mini-executor (`agent_tools/demo.py`, ~50 lines, uses claude-agent-sdk with these tools) that: creates a run on assure42 with a simple feature request, waits, prints open questions, approves with defaults, fetches the plan, walks next_ready_tasks printing execution order with get_task_context sizes. Report its transcript.
- 6 golden cases captured; `run_evals` green against them.

## Out of scope

Actual code-writing executor logic, multi-agent orchestration, CI wiring, embedding-based scoring, UI changes.
