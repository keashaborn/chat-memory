# Memory V1 V5 sanitized shadow-trace persistence

`memory.v5_shadow_trace_event` is a private, owner-scoped, append-only audit stream for the zero-influence V5 shadow selector. It records hashes, bounded counts, routing outcomes, rejection codes, budgets, and fixed zero-write/zero-influence assertions.

It cannot store query text, messages, prompts, answer text, claim prose, evidence prose, or system-prompt content. `brains_app` has no direct table privileges. It may only execute `memory.record_v5_shadow_trace_v1(...)`, whose restricted security-definer role derives the owner from transaction-local `app.user_id`, forces RLS, validates every field, rejects mismatched replay, and writes at most one row per owner/request hash.

Runtime persistence is independently gated by `MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE`. The V5 shadow selector remains read-only and has no retrieval activation, prompt injection, answer-model exposure, Qdrant writes, or governed-memory writes.

Production validation requires a fresh database backup, the production-clone security suite, an initially empty trace table, a bounded ordinary-use canary, zero governed-state changes, unchanged Qdrant state, and owner-isolation checks. Trace rows are operational audit records; they are not memory evidence or retrieval candidates.
