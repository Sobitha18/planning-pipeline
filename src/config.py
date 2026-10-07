"""Settings from env. DATABASE_URL plus LLM model routing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# repo root .env (two levels up: src/ -> planning-pipeline/ -> dev_agent/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    # LLM_API_KEY, not ANTHROPIC_API_KEY: dev_agent's claude_agent_sdk hands
    # its whole process env to the Claude Code CLI, and a set
    # ANTHROPIC_API_KEY there overrides the operator's OAuth login (metered
    # billing; a stale key hangs the run on an approval prompt nobody
    # answers). One .env feeds both apps, so the key uses a name the CLI
    # does not look for and is passed to anthropic.Anthropic explicitly.
    llm_api_key: str | None
    llm_cheap_model: str
    llm_strong_model: str
    # Escalation thresholds (task 07). Not failures — the size past which the
    # pipeline says "this is bigger than I handle well". Right size varies per
    # repo/team, hence env-tunable without a code change.
    max_plan_tasks: int
    max_touchable_files: int
    # Gate 2 chat's turn cap per plan version — same tunable-threshold pattern
    # as the two above, not a schema limit.
    max_chat_turns: int


def get_settings() -> Settings:
    """Reads env every call — cheap, and lets tests monkeypatch env vars
    without needing to reset a cached singleton."""
    return Settings(
        database_url=os.environ.get("DATABASE_URL"),
        llm_api_key=os.environ.get("LLM_API_KEY"),
        llm_cheap_model=os.environ.get("LLM_CHEAP_MODEL", "claude-haiku-4-5"),
        llm_strong_model=os.environ.get("LLM_STRONG_MODEL", "claude-opus-5"),
        max_plan_tasks=int(os.environ.get("MAX_PLAN_TASKS", "10")),
        max_touchable_files=int(os.environ.get("MAX_TOUCHABLE_FILES", "25")),
        max_chat_turns=int(os.environ.get("MAX_CHAT_TURNS", "12")),
    )
