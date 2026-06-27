# Memory Architecture Audit

## Current Finding

Verbal Sage currently has multiple memory/card systems operating in parallel.

## Active Memory Layers

### 1. Qdrant memory_raw

Vector-backed memory collection. Stores episodic memory, legacy memory_card records, user_identity cards, style cards, gravity_profile, vb_desire_profile, and feedback metadata.

Used by retrieve_personal_memory() in rag_engine/retriever_unified.py.

### 2. Postgres Vantage Cards

Newer Vantage-scoped structured card system stored in vantage_card.card_head.

Loaded by build_vantage_preference_cards_block() and build_vantage_profile_cards_block() in rag_engine/persona_loader.py.

### 3. Derived Profile Systems

Includes gravity_profile, vb_desire_profile, and style/persona refresh mechanisms.

These should be treated as experimental or supporting systems until their runtime effect is verified.

## Current Prompt Injection Path

Vantage query builds a system prompt from overlay text, Vantage preference cards, Vantage profile cards, retrieved Qdrant personal memory, and retrieved corpus chunks.

In vantage_router.py, build_system_prompt() is called with include_persona=False, so the older full Qdrant persona block is not injected through build_persona_block() in the normal Vantage path.

## Current Safeguards

- personal memory retrieval excludes assistant chat
- excludes gravity_daemon and vb_desire_daemon cards
- excludes legacy memory_card records from episodic retrieval
- filters by active vantage_id
- allows legacy fallback only for default Vantage unless enabled
- filters obvious test/probe prompts
- drops the just-asked message

## Architecture Direction

- Keep Qdrant memory_raw for episodic vector retrieval.
- Keep Postgres Vantage cards as canonical structured/profile cards.
- Treat gravity_profile and vb_desire_profile as experimental until verified.
- Avoid letting legacy Qdrant memory_card records duplicate or compete with Postgres Vantage cards.
- Any memory that affects an answer should be visible in Inspector, at least in summarized/debug form.

## Audit Questions

1. What writes to each memory/card layer?
2. What retrieves from each layer?
3. What gets injected into the live prompt?
4. What is canonical versus legacy?
5. What is valuable but not active?
6. What creates noise or unrelated context?
7. What should be retired, gated, or hardened?
