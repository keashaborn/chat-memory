# Memory V1 ordinary-trace audit and universal shadow rollout

Date: 2026-07-14

## Scope

Read-only review of all 152 owner-scoped rows in `memory.retrieval_trace` before broadening Memory V1 beyond the primary owner. Conversation text is not stored in retrieval traces; ordinary requests were identified by matching the trace request ID to the same owner's `public.chat_log` user row.

## Trace classification

- Total trace rows: 152.
- Controlled or unmatched probe rows: 121.
- Ordinary-use trace rows: 31 across 29 user requests.
- Ordinary governed-claim traces: 12, selecting 14 claims.
- Ordinary specialized preference/project traces: 19. Sixteen skipped and three selected one project record each.

## Findings

Governed selection was correct and useful for direct mother-death recall, name correction, pet-loss recall, and caregiving support. One broad pet question selected only one of several relevant pet-loss claims. Two turns produced clear cross-topic selections: a childhood-location narrative selected three pet-loss claims, and a marriage/stepson narrative selected a caregiving-burden claim. Four information-providing turns reselected facts already present in the current message; this is relevant but redundant and risks injecting stale wording over newer evidence.

Specialized runtime v3 injected three ordinary-use project records. The behavior-change/A-B-design record was directly relevant. An adaptive-learning question selected an older hybrid-memory project record that can be stale relative to Memory V1. An API/data-source question selected the A-B-design record with lexical score 2 and was not directly relevant. This lane is not ready for broader answer influence.

For 17 ordinary specialized v3 traces, evaluation latency was 50.134 ms average, 57.714 ms median, 64.327 ms p95, and 69.830 ms maximum.

## Security gates verified

- `memory.retrieval_trace` and `memory.retrieval_trace_item` both have RLS enabled and forced.
- Both tables enforce `owner_user_id = memory.current_actor_user_id()` for read and write.
- The `/vantage/query` caller verifies actor UUID equals body owner UUID before either Memory V1 runtime is called.
- The actor-auth mismatch test passes.
- Trace metadata contains identifiers, decisions, scores, and hashes, but no query text or selected canonical text.

## Rollout decision

Broaden audit-only shadow evaluation to every authenticated, actor-verified UUID. Do not broaden prompt influence.

The runtime change is controlled by `MEMORY_V1_SHADOW_ALL_AUTHENTICATED=1`. `MEMORY_V1_GOVERNED_ACTIVE_USER_IDS` and `MEMORY_V1_SPECIALIZED_ACTIVE_USER_IDS` remain separate explicit activation lists. Enabling universal shadow therefore cannot activate governed or specialized prompt blocks for another account.

## Activation blockers

Before expanding answer influence beyond the primary owner:

1. Suppress retrieval on information-providing turns unless contradiction or normalization is required.
2. Fix broad-recall completeness for multi-record questions.
3. Require stronger project-lane relevance than a low lexical overlap.
4. Add document authority/freshness to project selection so superseded architecture records cannot win.
5. Review ordinary traces from at least one additional active account while prompt exposure remains zero.
