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
