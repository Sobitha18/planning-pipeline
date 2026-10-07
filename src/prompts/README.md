# Prompts

Prompts are files, not string literals — diffable and eval-able. Loaded with
`src.llm.load_prompt(name)`, which reads `src/prompts/<name>.md`.

- `entity_extraction.md`, `rerank.md` — Stage A (context building).
- `spec_only.md` — gate 1: request + pack -> spec + open questions.
- `plan_only.md` — gate 2: request + pack + the APPROVED spec -> the task DAG.
- `plan_chat.md` — gate 2's chat: prose Q&A about an already-generated plan.
  Not a system prompt shared with `plan_only.md` — it never sees the context
  pack, only the spec/plan/validation-report facts `render_chat_prompt`
  renders for it.

`spec_only.md` and `plan_only.md` share a preamble (pack tier semantics, stack
inference) on purpose: they are two independent calls, and neither may assume
the other's system prompt was ever in context.
