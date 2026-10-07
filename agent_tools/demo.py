"""Scripted mini-executor: drives one real planning run end to end through a
live Claude Agent SDK session using ONLY the planning_tools MCP tools.

Requires: the planning API server running (PLANNING_API_URL, default
localhost:8000), assure42 registered + indexed, and LLM_API_KEY set.

    python agent_tools/demo.py
"""

from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
    TextBlock, ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, tool,
)

from agent_tools.planning_tools import TOOLS, _FN_BY_NAME

REPO_PATH = "/Users/anuprasjadhav/PycharmProjects/assure42"
REQUEST = "Add a CSV export button to the dashboard that lets a user download the current table view."


def _sdk_tool(entry: dict):
    fn = _FN_BY_NAME[entry["name"]]

    @tool(entry["name"], entry["description"], entry["input_schema"])
    async def handler(args, _fn=fn):
        return {"content": [{"type": "text", "text": json.dumps(_fn(**args), default=str)}]}

    return handler


async def main() -> None:
    server = create_sdk_mcp_server(name="planning", tools=[_sdk_tool(e) for e in TOOLS])
    options = ClaudeAgentOptions(
        mcp_servers={"planning": server},
        allowed_tools=[f"mcp__planning__{e['name']}" for e in TOOLS],
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                           "WebFetch", "WebSearch", "Task"],
        permission_mode="bypassPermissions",
        system_prompt=(
            "You are an executor agent. Use only the planning MCP tools, strictly in order: "
            "(1) create_planning_run; (2) wait_for_status for ['awaiting_spec_approval','failed']; "
            "(3) print the open_questions from that snapshot's spec; "
            "(4) approve_spec with the current spec version and no answers, so unanswered "
            "questions bind to their defaults — this starts plan generation; "
            "(5) wait_for_status for ['awaiting_approval','failed']; "
            "(6) approve_plan with the current spec_plan version; (7) get_approved_plan; "
            "(8) starting from an empty completed set, repeatedly call next_ready_tasks and print "
            "each batch as the execution order, then call get_task_context on every task in the "
            "batch and print the byte size of each returned file's content string (len() of it; "
            "null content = 0, a new file) — compute this yourself from the get_task_context "
            "result, no other tool — before adding that batch to completed and calling "
            "next_ready_tasks again until it returns no more tasks. Narrate each step."
        ),
        max_turns=30,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"repo_path={REPO_PATH!r}, request_type='feature', request_text={REQUEST!r}")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"[assistant] {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"[tool_use] {block.name} {json.dumps(block.input)}")
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        print(f"[tool_result] {str(block.content)[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"[result] turns={msg.num_turns} cost=${msg.total_cost_usd}")


if __name__ == "__main__":
    asyncio.run(main())
