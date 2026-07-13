# Verbal Sage Memory System Audit Ledger

Audit date: 2026-07-12

Status: architecture approved. Memory V1 foundation is deployed in parallel; legacy runtime remains live while V1 is tested in shadow mode.

## Target invariants

1. Factual memory is owned by the authenticated Supabase `user_id`, never by a Vantage, persona, browser cookie, or thread.
2. Persona/Vantage settings may affect rendering but do not own, partition, or change factual memory.
3. Every backend memory read and write enforces actor/owner equality; the frontend is not the sole security boundary.
4. No cross-user retrieval, mutation, export, feedback, or debug visibility is possible.
5. Raw evidence, governed facts, response preferences, and internal job state are separate stores or strictly separate schemas.
6. Raw evidence remains auditable. Promotion, correction, merge, contradiction, supersession, retirement, and deletion are traceable.
7. Retrieval selects the smallest relevant governed fact set. Unrelated available memory is not injected.
8. Facts carry explicit entities, relations, temporal validity, provenance, evidence, confidence, sensitivity, authority, and contradiction state.
9. Importance, repetition, recency, and answer-use are separate signals. Repetition does not automatically make a claim true.
10. Background jobs improve specificity and organization without silently rewriting history or weakening security.

## Confirmed architecture

- Qdrant `memory_raw` contains high-recall conversational/episodic evidence and legacy card/profile records.
- Postgres `vantage_card.card_head`, `card_revision`, `card_link`, and `card_signal` contain structured cards, revisions, provenance references, and use signals.
- Candidate extraction is read-only in `scripts/personal_event_inventory.py` and `scripts/profile_fact_inventory.py`.
- `scripts/review_promotion_plan.py` produces review actions and has a guarded single-card create path.
- Four reviewed durable personal cards exist: DeeDee death, Monika caretaking context, Neko correction, and Helsing pet loss.
- `rag_engine/vantage_router.py` reads approved active durable personal cards, applies deterministic matching, injects selected summaries into the system prompt, and records `injected_into_prompt` signals.
- The Admin Memory Review and Memory Inspector are read-only observability surfaces.
- Normal frontend chat derives `user_id` from verified Supabase authentication and sends the same ID in the Brains actor header and request body.
- Brains requires an infrastructure service token on `/vantage/query`.

## Confirmed problems and risks

### Identity and security

- Qdrant was published on every host interface. On 2026-07-12 it was changed to `127.0.0.1:6333`; persisted collections recovered green with `memory_raw=570` and `memory_claim_v1=4`, and a Verbal Sage private-VPC probe timed out.
- Redis was published on every host interface. On 2026-07-12 it was changed to `127.0.0.1:6379`, its persisted volume was retained, and a Verbal Sage private-VPC probe timed out.
- The Brains deployment Compose file contains database credentials directly instead of using an external secret. Rotation and dependency-safe secret migration remain required.
- `/vantage/query` now requires exact UUID equality between `x-vs-actor-user-id` and request-body `user_id`. Production probes return 401 for missing actor, 403 for mismatch, and 200 for a matching authenticated actor.
- `/log` now requires the same exact actor/owner UUID equality before Postgres or Qdrant writes and no longer Vantage-remaps the owner for new transcript, identity, or raw-memory records.
- The deployed frontend currently sends a verified Supabase ID, but Brains lacks defense in depth against a buggy or compromised service-token holder.
- `resolve_canonical_user_id()` keys aliases by `(vantage_id, alias_user_id)`, so canonical account identity remains persona-dependent.
- Durable personal records use `vantage_id='user_global'` while embedding the actual user ID inside `topic_key`; ownership is not a constrained column.
- Live catalog data contains similar identity/background facts under multiple user IDs and Vantages. Whether these are old aliases or distinct accounts still requires verification; they must not be merged by content alone.

### Store and schema contamination

- One generic card schema mixes factual identity/background/project data, style preferences, personal events, corrections, audit records, and internal consolidation cursors.
- 108 card heads exist: 66 active and 42 inactive. Only four are reviewed durable personal cards.
- Active low-quality extractions include `pref/https` and a development instruction stored as `pref/next_likely_step`.
- Exact or near-exact identity/background/project facts are duplicated across Vantages and historical runs.
- Approval, sensitivity, use scope, and surface policy are mutable JSON fields rather than constrained governance columns.
- Provenance is attached at card level, not to individual claims.
- No first-class entities, typed relations, temporal bounds, contradiction sets, supersession, or evidence authority exist.
- `card_revision.prev_revision_id` is not an enforced foreign key, and the head duplicates mutable summary/payload state.
- `strength` plus reward/punishment signals are inherited from behavioral learning and are not yet separated from factual confidence or importance.
- The `/log` request path executes `CREATE TABLE IF NOT EXISTS` and multiple `ALTER TABLE` statements during ordinary message writes; schema management is mixed with runtime ingestion.
- Postgres transcript writes are called authoritative, but exceptions are swallowed. The request may continue to Qdrant and return success after a Postgres failure, allowing silent store divergence.
- The special `frontend/identity` path writes a legacy Qdrant `memory_card` and returns without an equivalent authoritative Postgres transcript write.
- An existing `vantage_fact` schema contains source, entity, predicate, claim, evidence, and contradiction tables, but none has an explicit owner column.
- `vantage_fact` currently contains 600 sources, 600 entities, 711 claims, 712 evidence rows, and 3 contradiction groups. Chat sources represent 22 stored user IDs.
- Fact extraction is Vantage-scoped through `source.metadata.vantage_id`; claims are scoped only indirectly through evidence links.
- Extracted subjects are document entities, and claims use a globally unique canonical key. This is unsuitable as the account-owned canonical memory store.
- Existing `vantage_fact.source` rows may be useful as migration evidence when `metadata.user_id` is a valid authenticated account ID. Existing extracted claims should not be trusted or migrated wholesale.

### Retrieval and prompt pollution

- Runtime durable-card matching is hard-coded keyword logic for the current few scenarios.
- The query loads at most 12 high-strength cards before relevance matching; relevant lower-ranked cards can become unreachable as the catalog grows.
- Runtime selection does not visibly enforce sensitivity, authority, temporal validity, contradiction status, or claim-level provenance.
- A `MEMORY_ARCHITECTURE` turn injected Helsing loss, Monika caretaking, and the Neko correction, demonstrating unrelated prompt pollution.
- Code comments and debug metadata still describe the selector as debug-only even though it performs live prompt injection.
- The prompt receives flattened card summaries rather than a compact verified relational subgraph.
- Signals prove selection/injection, not actual answer use, correctness, usefulness, or harm.
- Qdrant personal retrieval is user-filtered by `payload.user_id`, which is a useful store-level isolation control once the caller identity is trusted.
- Vantage filtering for episodic memory is disabled by default, consistent with account-global memory; legacy strict Vantage behavior remains behind environment flags.
- Ordinary raw user chat is directly eligible for answer-time prompt injection based primarily on embedding similarity. It has not passed factual extraction, evidence comparison, contradiction handling, or promotion governance.
- The default raw-memory score threshold is low (`0.20`), and the search over-fetches 40–80 candidates before returning `top_k`.
- Ranking adds response-format, tone, intent, and legacy gravity bonuses. These behavioral similarities can outrank factual relevance and should not determine truth-bearing memory retrieval.
- Test/prompt pollution is controlled by lists of literal markers rather than an explicit record class or ingestion policy.
- A separate `score_personal_hit()` feedback scorer remains in the module, while live retrieval uses different inline scoring logic; this is duplicate/possibly dead policy code.
- The route audit passes its assertions, but several assertions certify current behavior rather than architectural correctness.
- `MEMORY_ARCHITECTURE` intentionally retrieves two raw personal messages plus one corpus chunk and injects compressed excerpts; this is useful history but still treats conversation text as answer authority.
- Audit metadata reports `raw_personal_memory_allowed=false` while personal raw hits are retrieved and represented in the prompt for `MEMORY_ARCHITECTURE`; the policy vocabulary is internally inconsistent.
- Durable-card recall probes still inject four additional raw personal hits alongside the governed durable card, increasing prompt size and allowing weaker evidence to compete with the approved fact.
- A family-death recall question is classified as `GENERAL`, showing that intent routing and durable-card policy use overlapping but inconsistent heuristics.
- The FM conceptual audit injects 16 thread messages totaling about 30,232 characters while declaring compression required. Thread context can dominate the prompt even when the personal archive is suppressed.
- Preference cards remain enabled for every audited route, including `TECH` and `FM_CONCEPTUAL`; polluted or obsolete style preferences therefore have broad reach.

### Legacy Vantage/persona controls

- User memory was historically Vantage/persona-scoped; the intended architecture now rejects that ownership model.
- Vantage response controls, cookies, learned reward/punishment behavior, persona cards, derived gravity/VB-desire profiles, and user facts remain intermingled.
- User-controlled response preferences may remain, but backend/internal control parameters and factual governance should not be editable as persona sliders.
- Active preference/profile loaders search the requested Vantage, `user_global`, and every legacy Vantage (`default`, `RESSE`, `EVA`, `RILEY`, `MORGAN`) for each request. This compatibility fan-in preserves cross-Vantage duplication and legacy behavior.
- Preference cards are loaded on every normal Vantage prompt, capped at eight before post-query topic filtering. Ignored cards can consume the SQL limit and hide legitimate lower-ranked preferences.
- Preference/profile card retrieval checks status and broad use scope but not review status, sensitivity, contradiction state, temporal validity, or provenance.
- Profile cards default to inclusion for `GENERAL` turns unless a narrow suppression heuristic matches. Generic chat can therefore receive up to ten biography/project cards without a demonstrated need.
- Profile/preference deduplication uses full `kind:topic_key`; duplicates stored under different historical topic keys or conflicting values are not semantically reconciled.
- The normal Vantage path disables the full legacy persona block but still injects legacy user instructions, request overlays, preference cards, conditional profile cards, raw/corpus chunks, and the newer durable-card block through separate paths.
- `format_memory_chunks()` labels source collections but does not convey claim authority, evidence quality, contradiction status, or why each item was selected.

## Direction supported by evidence

- Do not keep layering filters onto the generic Vantage card system as the final design.
- Define an account-owned relational fact/evidence model with strict Supabase user ownership.
- Preserve `memory_raw` initially as immutable/high-recall evidence, but stop treating raw conversation similarity as sufficient authority for prompt injection.
- Use governed facts/relations as the normal answer-time source; retrieve evidence only when verification or detail requires it.
- Separate response preferences from factual memory and separate both from internal job state.
- Migrate only verified useful records. Compare the new retrieval path in shadow mode before retiring legacy reads.
- Keep the inspector read-only; introduce mutation only through explicit authenticated governance operations with an audit trail.

## Memory V1 implementation status

- The `memory` Postgres schema is account-owned by `owner_user_id uuid`, with forced row-level security and transaction-local `app.user_id` actor context.
- Evidence, entities, claims, claim/evidence links, relations, assessments, revisions, candidates, preferences, retrieval traces, and projection outbox are separated.
- Six governed claims now exist for the exact authenticated owner: DeeDee death, Monika caregiving context, Neko name correction, Helsing loss, Neko loss, and Dahlia loss. The Neko loss depends on the explicit Neko-not-Nemo correction claim. Legacy records were not changed.
- Candidate application is proposal-hash locked, extractor allowlisted for automatic approval, revisioned, and supersession-aware.
- Retrieval rechecks every Qdrant candidate under Postgres RLS, then applies status, validity, sensitivity, domain, intent, surface, explicit-recall, claim-count, and token-budget gates.
- The Qdrant V1 projection uses a separate `memory_claim_v1` collection. Payloads contain owner, claim ID, revision, and policy metadata only; personal prose stays in Postgres.
- Projection processing is owner-explicit, uses `FOR UPDATE SKIP LOCKED`, avoids external calls inside database transactions, and does not lose a concurrently refreshed outbox job.
- The isolated schema/store/retrieval/projection test suite passes. Live prompt routing has not been switched to V1.
- Exact actor/body UUID enforcement for `/log` and `/vantage/query` passes helper, route-level container, and production probes and is deployed at backend commit `18001ac`.
- Production Qdrant and Redis host ports are loopback-only. Redis was checkpointed before recreation. Durable post-change Qdrant snapshots with checksums are stored under `/home/ubuntu/brains/snapshots/20260712_memory_v1`; Brains health passed after recreation. The six-point `memory_claim_v1` snapshot is `memory_claim_v1-6878092170277446-2026-07-13-03-43-36.snapshot` (SHA-256 `695d1c317587ac3c6379d645d617cf0999abe1aef3b8d29b2cb24233a37000ff`).
- Allowlisted, non-injecting route shadow evaluation is deployed for owner `1240822d-ac9a-4096-95aa-e2b24d36ef50`. Controlled probes selected exactly one claim for name correction, family loss, pet loss, and caregiving context; technical and unrelated nutrition turns created no V1 trace.
- Controlled shadow probe latency was approximately 0.79–1.07 seconds per inspected turn. Before broader rollout, reuse the route's existing query embedding or otherwise remove the duplicate embedding request.
- The first ordinary-use shadow sample contained 11 user turns: six governed-domain turns each selected exactly one intended claim, four negative controls correctly created no V1 trace, and one implicit family-event query was initially missed. The miss (`What happened with my mom?`) is fixed and now selects the DeeDee claim without classifying unrelated questions about the user's mother.
- A legacy pet-loss answer used the explicitly corrected wrong name `Nemo` and combined several raw memories. The reviewed source was hash-locked and promoted transactionally into separate Neko and Dahlia loss claims; Helsing received the corroborating evidence. The pre-change Postgres backup is `/home/ubuntu/memory_data_pre_pet_normalization_20260712.sql` (SHA-256 `2e1f007fe7bc17c843de0911793952e130493115f6172688c62b9adbc52dca39`). The guarded migration is deployed at `c3f86ff`.
- Named pet-event routing is distinct from spelling correction routing at `9f845a2`. Structured entity gating is deployed at `5977da6`: entity hints are compared only with governed structured fields, never arbitrary prose. Generic pet recall selected Helsing, Neko, and Dahlia; named Neko and Dahlia probes each selected only their matching event; the spelling probe selected only the correction. Production trace IDs are `1c3111b0-191c-4940-9983-76ed63ae8b36`, `c2e5e0b3-e690-47d5-b3e4-6d0b62011b08`, `0cb40eb6-a74a-4c91-9a80-77dd249f8203`, and `122a7cf2-7532-4b8c-8ea6-e62a1df076cf`.
- The complete Docker-isolated Memory V1 suite passed after entity gating: idempotent schema application, forced-RLS tenant isolation, actor authentication, store and projection integrations, shadow policy, reviewed-pet normalization, schema dump, and guarded rollback. Production remained allowlisted and non-injecting.
- A recent-macros question received a generic “cannot access your data” response even though nutrition is a structured application domain. This is a structured nutrition-routing/integration defect, not a Memory V1 retrieval failure.

## Unresolved checks

1. Inspect the raw `/log` write path and Qdrant personal retrieval filters on seebx.
2. Verify all Brains memory routes enforce actor/owner equality, not only service-token access.
3. Map every active prompt contributor: raw personal retrieval, profile cards, preference cards, overlays, gravity/VB desire, thread context, and durable personal cards.
4. Determine whether repeated user IDs are historical aliases for one account or truly distinct accounts without exposing or merging data.
5. Inspect live Qdrant payload identity fields, legacy fallbacks, source exclusions, and Vantage filters.
6. Inventory Supabase/Vantage cookie controls and classify each as user preference, admin diagnostic, internal policy, or obsolete.
7. Verify deletion/export semantics cover raw evidence, governed facts, revisions, links, signals, traces, and derived embeddings.
8. Define the target schema and migration/cutover plan only after the architecture discussion.
