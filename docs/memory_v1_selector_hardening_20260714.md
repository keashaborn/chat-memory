# Memory V1 selector hardening — 2026-07-14

## Scope

This change corrects four retrieval defects without changing authentication,
ownership, durable memory, or rollout allowlists:

1. Declarative information-providing turns could retrieve redundant personal
   claims.
2. Broad personal recall did not retrieve the complete relevant pet set.
3. Project retrieval accepted weak text-only matches with no project-key
   anchor.
4. Historical project records could answer current planning or status turns.

The first production inspect-only probe then exposed a fifth contributor: the
retired `vantage_card` durable-personal selector could still inject its legacy
prompt block whenever the governed selector correctly skipped a declarative
turn. The obsolete selector, preview, prompt formatter, and injection-signal
writer were removed from `rag_engine/vantage_router.py`. Existing legacy rows
are retained as inactive historical data; they are no longer read by this
answer path.

## Selector rules

- Governed personal retrieval requires an explicit recall/normalization cue or
  a support need. A personal statement alone is not a retrieval request.
- Broad pet and family recall are explicit governed-memory routes.
- Name normalization is limited to spelling/correction requests; a question
  about what happened to Neko remains a pet-loss recall.
- Project records require direct relevance, an intent-compatible knowledge
  kind, a project-key anchor, and a minimum deterministic score.
- Historical records are eligible only when the query explicitly asks for
  history or legacy behavior.
- Current document state and authority level are deterministic tie-breakers.

## Security invariants

- `x-vs-actor-user-id` must equal the request owner UUID.
- Retrieval transactions set the database actor locally.
- Every Memory V1 owner table remains protected by enabled and forced RLS.
- Owner policies compare `owner_user_id` to
  `memory.current_actor_user_id()`.
- Specialized retrieval rejects any cross-owner snapshot row in application
  code in addition to database RLS.
- Prompt activation remains allowlisted to the existing primary account.
  Universal authenticated shadow tracing remains inspect-only for other
  accounts.
- The answer path no longer reads or injects legacy durable personal cards
  from `vantage_card.card_head`.

## Deterministic gates

- Intent, governed-policy, specialized-policy, project-retrieval, pet-review,
  actor-authentication, and embedding-cache tests.
- Rollback-only production Postgres RLS test covering cross-owner reads,
  writes, links, missing-actor behavior, and forced-RLS metadata.
- Inspect-only activation probes for positive recall, broad recall,
  information-providing suppression, weak project matches, and historical
  filtering.
- Post-deployment trace visibility checks with the primary owner and a second
  authenticated owner before any prompt rollout is broadened.

No claims, evidence, preferences, project records, reviews, or source
artifacts are modified by this change.

## Production verification

- Selector hardening deployed at backend commit `40c84d2`; the obsolete
  durable-card selector was retired at `09402a7`. The verified production head
  after reusable probe commits is `cdcdca1`.
- Restore points are
  `/home/ubuntu/brains/snapshots/memory_v1_selector_hardening_20260714T211002Z`
  and
  `/home/ubuntu/brains/snapshots/memory_v1_legacy_selector_retirement_20260714T211836Z`.
- Eleven governed inspect-only cases passed. Broad pet recall selected exactly
  Neko, Dahlia, and Helsing. Three information-providing cases and the
  unrelated control selected zero and injected no prompt block.
- Five specialized inspect-only cases passed. The music and current roadmap
  cases selected their expected records. Adaptive learning, the unrelated API
  project case, and the general control selected zero.
- A second existing actor produced fresh governed and specialized traces with
  zero selections, zero prompt injection, and zero answer-model exposure. The
  trace IDs are `ba9b0ae0-0e1e-47e2-be7c-cc155525d7cf` and
  `027de4be-c8c9-4c4a-934a-5caf287cce58`. An actor/body owner mismatch returned
  HTTP 403.
- Under forced RLS, the second actor saw zero of the primary actor's fresh
  traces and the primary actor saw zero of the second actor's fresh traces.
  The second actor saw `0 claims / 0 preferences / 0 project records`; the
  primary actor saw only its own `6 / 3 / 5` rows.
- The second actor has no recent non-test retrieval traces. Prompt influence
  therefore remains primary-owner-only until ordinary use from another active
  account supplies a real-use trace sample.
