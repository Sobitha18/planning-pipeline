# Task Prompts — How to Run These with Claude Code

Folder contents (one task = one Claude Code session):

```
01-extractor.md          ✅ done
02-indexer.md            (in progress / done)
03-edges.md              symbol graph edges
03b-prisma.md            prisma schema models (mini-task; after 02, independent of 03)
04-llm-gateway.md        LLM choke point
05-context-engine.md     Stage A
06-generator.md          Stage B generation
07-validators.md         validation + repair (accuracy core)
08-runs-api.md           state machine + HTTP API
09-approval-ui.md        thin webapp
10-agent-tools-evals.md  agent SDK tools + golden evals
```

Dependency order is strict: 03 → 04 → 05 → 06 → 07 → 08 → 09 → 10. (03, 03b, 04 are mutually independent — any order; 03b can share a session with 03 if capacity allows.)

## Locked decisions (from planning)
- Deployment: server; repos cloned to server disk out-of-band, registered via POST /v1/repos {root_path}; re-index after git pull via /reindex
- No auth in v1 (internal single-team tool)
- Executor agents: same server, filesystem access to repos
- Repos are independent (no cross-repo planning)
- Prisma schema parsing: in scope (03b)
- Dual-language (TS + Python) is real: a production Python repo will be onboarded — keep everything language-agnostic
- Escalation thresholds configurable via env (MAX_PLAN_TASKS=8, MAX_TOUCHABLE_FILES=12 defaults)
- Vector DB: none in v1; pgvector later only if Stage A smoke shows recall gaps
- Models: strong=Sonnet-class, cheap=Haiku-class, env-overridable in config

Test repos referenced by every smoke test:
```
/Users/anuprasjadhav/PycharmProjects/assure42
/Users/anuprasjadhav/PycharmProjects/assure42-clinical-ops
```

---

## Running within session/usage limits

**1. One task per session, always.** Each prompt is scoped to fit a session. Never say "now continue with the next task" in the same session — start fresh. Fresh sessions also give Claude Code a clean context, which improves quality more than continuity helps.

**2. Starting a session:**
```
claude
> Read docs/design.md for overall context, then implement the task in
> task-prompts/03-edges.md exactly as specified. Run the tests as you go.
> Do NOT run any git commands — no commits, no branches; leave version
> control entirely to me.
```
Keep `docs/design.md` (the locked design) in the repo — it's the shared context so each prompt doesn't need to re-explain the system.

**3. The cheap-session discipline (this is what saves your quota):**
- Let Claude Code write code + unit tests and iterate until `pytest -q` is green. That's the session's job.
- **Run the real-repo smoke tests YOURSELF after the session ends** (each prompt's smoke section is a copy-pasteable script/command). LLM-calling smoke tests (tasks 05-08) burn both Claude Code quota AND Anthropic API tokens if done inside the session.
- Paste smoke failures back as a NEW short session: "Task 05 smoke failed with this output: <paste>. Fix." — a targeted fix session is 10x cheaper than keeping the build session alive.

**4. Compaction control:** if a session runs long, tell it to `/compact` after tests go green and before any refactor passes. If you hit the limit mid-task, start a new session with: "Task 06 is partially done. Run pytest, read the failures and task-prompts/06-generator.md, finish the remaining work." Claude Code re-derives state from the repo + tests — that's exactly why every task requires tests.

**5. No commits by Claude Code — ever.** It must not run git commands. YOU review the diff and commit (or discard) after verifying each task; that keeps version control decisions human. Reviewing then committing per task still gives you the cheap-rollback property.

**6. Model choice per task:** tasks 03, 04, 08, 09, 10 are mechanical — Sonnet is fine and cheaper on limits. Tasks 05, 06, 07 (retrieval logic, the core prompt, validators) benefit from Opus/strongest available. Set per session.

**7. Never let it renegotiate scope.** Each prompt has an "Out of scope" section — if Claude Code proposes building something listed there, decline. If a test in the spec seems wrong to it, the rule stated in prompts 01-02 applies everywhere: the spec wins; report disagreement, don't self-modify the spec.

**8. Expected session count:** 9 build sessions (03-10) + ~3-5 short fix sessions from smoke feedback ≈ 11-13 sessions total.

## Per-task verification checklist (you, after each session)

- `pytest -q` green locally
- Run the task's real-repo smoke ON BOTH repos, eyeball per its checklist
- Review the diff yourself; commit or discard (your call, outside Claude Code)
- Only then start the next task
