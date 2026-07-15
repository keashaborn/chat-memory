# Memory Routing Map

Current checkpoint: 079d258 Add FM conceptual route

## Active Vantage path

vantage_router.py:
- classifies turn intent
- builds retrieval plan
- calls personal/corpus retrieval
- combines memory chunks
- builds debug metadata
- calls prompt_builder.build_system_prompt()

retriever_unified.py:
- performs corpus/vector retrieval

prompt_builder.py:
- assembles final system prompt
- gates profile cards
- formats retrieved memory
- compacts memory for MEMORY_ARCHITECTURE and FM_CONCEPTUAL

memory_route_audit.py:
- regression harness for routing, prompt injection, store gating, and debug metadata

## Current turn intents

TECH:
- suppress FM lens
- suppress personal archive
- suppress corpus
- suppress profile cards

MEMORY_ARCHITECTURE:
- suppress FM lens
- limited personal archive
- limited corpus
- suppress profile cards
- enable debug semantic previews

FM_CONCEPTUAL:
- allow FM lens
- allow conceptual corpus
- suppress broad personal archive
- suppress profile cards
- compact retrieved memory

SPECIFIC_RECALL:
- prioritize personal archive
- suppress corpus
- suppress profile cards
- allow raw answer-bearing personal memory

PROFILE_SUMMARY:
- allow profile cards
- allow personal archive
- allow corpus

## Prompt inspector principle

The final system prompt is the runtime phenotype of the memory system.

The main question is always:

- what was injected
- what was suppressed
- how much context was injected
- which store supplied it
- whether the final answer had the right behavioral conditions


## Semantic dedupe layer

Semantic dedupe is currently implemented in `rag_engine/vantage_router.py`.

### `semantic_dedupe_preview_v0`

Purpose: expose obvious duplicate retrieved chunks in debug metadata without changing behavior.

Current behavior:
- Runs after retrieval trim.
- Groups chunks by deterministic normalized question/text key.
- Exposes:
  - `input_count`
  - `cluster_count`
  - `duplicate_risk_count`
  - duplicate refs
  - canonical ref
  - cluster reason
- Does not write to storage.
- Does not mutate memory by itself.

### `semantic_dedupe_apply_v0`

Purpose: remove exact normalized-question duplicate chunks before prompt injection for narrow, tested routes.

Current behavior:
- Applies only when `turn_intent == "FM_CONCEPTUAL"`.
- Uses refs already identified by `semantic_dedupe_preview_v0`.
- Keeps canonical chunk.
- Keeps distinct non-clustered chunks.
- Exposes apply metadata:
  - `applied`
  - `input_count`
  - `output_count`
  - `removed_count`
  - `removed_refs`
  - `reason`

### Current regression probes

The route audit includes FM conceptual probes for:

- `Could you describe the origin of consciousness.`
  - Confirms dedupe reduces 4 retrieved chunks to 2 injected chunks.
  - Confirms personal archive and profile cards remain suppressed.

- `Could you describe the process of the emergence of consciousness from nonexistence?`
  - Confirms dedupe reduces 4 retrieved chunks to 3 injected chunks.
  - Confirms `non-being` and `recursive differentiation` remain in the final prompt.
  - Confirms personal archive and profile cards remain suppressed.

### Known parked issue

FM conceptual retrieval and dedupe are now observable and controlled, but answer generation can still drift if the Vantage personality overlays introduce unrelated behavioral, control-theory, psychological, or predictive-processing synthesis. That is intentionally parked for a later `FM_CONCEPTUAL` answer-discipline pass.

## Personal memory card pipeline preview

Read-only preview pipeline now exists in scripts/personal_event_inventory.py.

Pipeline: memory_raw -> personal-event candidates -> canonical card candidates -> correction candidates -> merged card candidates -> review decision preview -> promotion mapping preview -> policy retrieval preview.

Detected categories: death_loss, pet_death_loss, caretaking_burden, relationship_anchor, name_alias_correction.

Current test result: for the question Have I had any deaths in my family recently?, policy retrieval selects the DeeDee death card and rejects unrelated pet-loss, relationship-anchor, and caretaking cards.

Durable store mapping targets the existing vantage_card schema: card_head, card_revision, and card_link. All generated rows are still preview-only with write_intent=none_preview_only.

Known limitations: deterministic narrow extraction, multi-event memories need split review, correction relevance is over-broad, no durable writes yet, no live card retrieval wired into vantage_query yet, and answer-use/outcome logging is still pending.

## Proposed V5 relational extraction contract (not runtime-active)

The reviewed V4 newest-25 evaluation is now bound to:

- `docs/MEMORY_V1_RELATIONAL_EXTRACTION_CONTRACT_V5.md`;
- `docs/MEMORY_V1_OBSERVATION_PERSISTENCE_DESIGN_V5.md`;
- `docs/MEMORY_V1_PREDICATE_REGISTRY_V5.md`;
- `docs/MEMORY_V1_TEMPORAL_CONTRACT_V5.md`;
- `docs/MEMORY_V1_ENTITY_RESOLUTION_REVIEW_CONTRACT_V5.md`;
- `specs/memory_v1_predicate_registry_v5.json`;
- `specs/memory_v1_entity_resolution_review_v5.schema.json`;
- `specs/memory_v1_relational_extraction_v5.schema.json`;
- `evals/memory_v1_relational_extraction_v5_cases.jsonl`;
- `evals/memory_v1_temporal_entity_resolution_v5_cases.jsonl`.

Source manifest SHA-256:
`8d31688923f3a0bb82c019b98dc6a78a867129a44157e80efc65432b60d2b649`.

Predicate registry canonical SHA-256:
`4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4`.

Temporal/entity contract hashes:

- model schema: `6292b44788bbdf652000b79668d18fd065747f9607380b7bfaf21f0a9b2cb9f7`;
- resolver schema: `b7b3d17056cbd10590743de9f183b49e5a4b1d62abba4926bd93aa6784574cb7`;
- 25-case fixture: `332783f8d293b607ae7b2a6006da40bf4f04778c94e6bc46fa24693099a70a4b`.

V5 proposes an evidence -> atomic observation -> entity/relationship resolution
-> governed claim -> policy -> projection flow. It preserves intent-specific
store permissions, technical/FM personal-memory suppression, typed surface
policy, owner-scoped retrieval, small budgets, compression, semantic dedupe,
prompt auditability, answer-use attribution, and isolated legacy fallback.

The observation boundary is now decided: immutable evidence-backed
observations require a dedicated owner-scoped store. The mutable
`memory.candidate` table remains hash-locked review/apply workflow state. The
proposed registry enables exactly 25 typed V5 predicates, keeps the 19 current
production predicates readable as non-emittable cutover compatibility, and
defers every unknown predicate.

Temporal and entity boundaries are also frozen. Calendar precision uses
half-open `daterange`; timezone-aware intervals use half-open `tstzrange`;
relative offsets retain their source form instead of becoming fabricated
instants. Model entity mentions no longer contain durable IDs, keys, or
resolution actions. A separate trusted owner-scoped resolver produces the
hash-locked review packet.

V5 now also has an executable, runtime-inactive relational staging migration and
a separately verified controlled-writer design. The staging migration creates
append-only mention, resolution, observation, temporal, and binding tables. The
writer design adds a constrained `NOLOGIN` owner role plus five hash-locked APIs
for packet staging, review preflight/apply, and resolution preflight/apply.

The application role is not a member of the writer role and has no direct access
to V5 owner tables. Every write API requires a `brains_app` session, a
transaction-local actor, forced owner RLS, exact packet hashes, and a replay-safe
manifest. Automatic non-self links recheck owner-local name/type uniqueness at
apply time. Named entity creation requires manual approval. Role-only and
anonymous creation remain blocked.

Both PostgreSQL 16 reconstruction and a production-schema-only clone pass the
full migration, adversarial transaction, replay, isolation, and rollback suites.
No production schema or data was changed.

V4 remains the active consolidation extractor, main-account consolidation
remains disabled, and no V5 extraction, projection, Qdrant, retrieval, prompt,
or legacy-cutover behavior is active. Before activating the V5 writer, legacy
direct `brains_app` mutation grants on durable pre-V5 tables must be audited and
revoked. The next design boundary is projection from applied observations into
governed claim, preference, and project views with full provenance.
