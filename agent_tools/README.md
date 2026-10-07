# agent_tools

Tool wrappers over the planning-pipeline HTTP API (`src/api.py`), for
executor agents built on the Claude Agent SDK to consume approved plans.

- `planning_tools.py` — plain, synchronous Python functions (httpx under the
  hood) + a `TOOLS` list of JSON-schema tool definitions + `_FN_BY_NAME`
  mapping tool name -> function.
- `demo.py` — a ~50-line scripted mini-executor that exercises the whole
  flow end to end against a real server.

Every function returns a JSON-serializable dict (or a list, for
`next_ready_tasks`'s success case). Errors — HTTP 4xx/5xx, network failures,
timeouts — come back as `{"error": {"status", "title", "detail"}}` rather
than raising, so an agent can branch on the value.

Set `PLANNING_API_URL` (default `http://localhost:8000`) before use.

## Registering the tools with `claude-agent-sdk`

The functions here are plain sync callables, not SDK tool objects — that
keeps them trivially unit-testable (mock `httpx.Client`, no SDK import
needed). Wrap them with `claude_agent_sdk.tool()` + `create_sdk_mcp_server()`
in the agent that uses them:

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
from agent_tools.planning_tools import TOOLS, _FN_BY_NAME

def _make_sdk_tool(entry):
    fn = _FN_BY_NAME[entry["name"]]

    @tool(entry["name"], entry["description"], entry["input_schema"])
    async def handler(args, _fn=fn):
        result = _fn(**args)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return handler

server = create_sdk_mcp_server(
    name="planning_pipeline",
    tools=[_make_sdk_tool(e) for e in TOOLS],
)

options = ClaudeAgentOptions(
    mcp_servers={"planning": server},
    allowed_tools=[f"mcp__planning__{e['name']}" for e in TOOLS],
)

async with ClaudeSDKClient(options=options) as client:
    await client.query("Plan a fix for the 500 error on the patients page.")
    async for msg in client.receive_response():
        ...
```

See `demo.py` for a complete, runnable version of this wiring driving one
scripted run end to end through both gates (create -> wait -> approve scope
-> wait -> approve plan -> walk the plan with `next_ready_tasks` +
`get_task_context`).

## Typical flow

Two human-approval gates: gate 1 approves the *scope* (spec + open
questions), gate 2 approves the resulting *plan*.

```
create_planning_run -> wait_for_status(["awaiting_spec_approval", "failed"])
  -> (inspect open_questions from get_run)
  -> approve_spec(version)                      # gate 1; unanswered questions bind to defaults
  -> wait_for_status(["awaiting_approval", "failed"])
  -> approve_plan(version)                      # gate 2
  -> get_approved_plan / get_run
  -> loop: next_ready_tasks(completed) -> get_task_context(task_id) per task
```

Hand-editing the spec directly (`POST /v1/runs/{id}/spec`) and chatting
about the plan are deliberately human-only — there are no agent tools for
either, since giving an agent that power would defeat the approval gate.
