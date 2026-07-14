# Memory V1 specialized runtime shadow deployment — 2026-07-14 UTC

## Scope

Server: seebx backend. Isolated branch: `memory_v1_foundation`. Production branch: `catalog_exercise_holds_defaultlog_v0`.

This phase installed the deterministic request-classification-to-`memory_intent` adapter and connected the durable preference/project selector to `/vantage/query` as an allowlisted measurement path. The path persists owner-scoped diagnostic traces but never adds selected content to the system prompt, model messages, or answer.

`response_mode`, persona, and `vantage_id` are absent from the memory-intent API, owner identity, database filters, and trace keys. RESSE remains responsible for response behavior and FM eligibility. Memory V1 remains responsible for governed claims, preferences, project knowledge, and structured application data.

## Runtime contract

The adapter is deterministic and fail-closed:

- Explicit music recommendation and preference-recall requests may inspect the `music` life-preference lane.
- Explicit project planning, status, decision, or recall requests may inspect only registered project `verbal-sage`.
- Existing personal claim routing uses the same adapter while preserving its prior domain policy.
- Unclassified and unrelated turns skip the specialized database path.
- Response preferences compile only into zero-token policy-control diagnostics.
- The runtime selector uses the locked maximums of three preferences, three project records, 500 estimated tokens, and high sensitivity.

The selector opens a repeatable-read, read-only transaction as `brains_app`, sets transaction-local `app.user_id`, requires enabled and forced RLS on all nine source tables, and filters by the authenticated Supabase UUID. A second short transaction inserts one `memory.retrieval_trace` row. The trace stores a user-bound query hash, typed intent, selected IDs/keys, counts, rejection codes, and policy-control diagnostics. It stores no query preview, canonical text, preference value, project prose, answer link, prompt content, or model output.

## Verification

Full isolated Memory V1 CI passed after the shared adapter and runtime wrapper were added. This includes forced-RLS tenant isolation, append-only evidence semantics, claim-shadow regression, specialized selection policy, projection, guarded rollback, kill-switch behavior, and trace-metadata redaction.

Production inspect-only probes passed:

| Case | Result | Selected governed records | Prompt exposure |
|---|---|---|---|
| Music recommendation | `recommendation` | Two music preferences | none |
| Memory feedback roadmap | `project_planning` | `memory.fractal_monism_feedback_loop` | none |
| Unrelated Mac popups | skipped | none | none |

All probes returned an empty answer and did not call the answer model. The two eligible probes created exactly two diagnostic traces. The unrelated probe created no specialized trace.

Production counts changed only in the trace lane: claims `6→6`, preferences `3→3`, project records `5→5`, projection outbox `6→6`, all retrieval traces `30→32`, and specialized traces `0→2`. A different owner context saw zero preferences, project records, or traces. Both specialized rows have `query_preview IS NULL`, `answer_id IS NULL`, `prompt_injection=false`, `answer_model_exposure=false`, and no `canonical_text` field in metadata.

## Deployment records

- Isolated commit: `e5bbdfbe3ba360ce6657a234fee083246893e863`
- Production commit: `5c4a6dc4308989eff4811dcce0cc4b76d9605c8c`
- Validated production backup: `/home/ubuntu/brains/snapshots/memory_pre_specialized_shadow_20260714T132502Z.dump`
- Backup size: `74707251` bytes
- Backup SHA-256: `ad9279394bac64a0c9a9b644447127465e778b412a5dc72df48d81c6923d3401`
- Prior systemd drop-in backup: `/home/ubuntu/brains/snapshots/30-memory-v1-shadow.conf.pre-specialized-20260714T132945Z`
- Active flag: `MEMORY_V1_SPECIALIZED_SHADOW=1`
- Active owner allowlist size: one Supabase UUID
- Brains after restart: active; authenticated `/healthz` returned `status=ok`; authenticated `/readyz` returned `postgres=true`.
- Deployment report: `MEMORY_V1_SPECIALIZED_RUNTIME_SHADOW_DEPLOY_20260714.json`, SHA-256 `a9579e7d01d3dfe9cacde1da02a0e6e2d2f91b87101f92c0684c8d65e3edd9e2`

The July 2026 Supabase change review found no blocking change for this direct PostgreSQL 16 path. The design does not depend on public Data API exposure, and it continues to require database-enforced RLS and application-role checks.

## Stop boundary

Ordinary-use collection is active, but only the two controlled traces existed at this stop. Do not inject preference/project selections into prompts, use them to change answers, or continue RESSE Phase 4 yet. The next step is to gather ordinary user turns and audit false positives, false negatives, selected-record minimality, latency, and cross-lane contamination before considering retrieval activation.
