# Router & Retrieval Architecture (seebx / rag_engine)

This document describes the current request routing, retrieval, and prompting behavior implemented under `rag_engine/`, based on the runtime code paths in:

- `rag_engine/rag_router.py`  (baseline RAG API)
- `rag_engine/vantage_router.py` (Vantage API: adds decision overlay + mix controls)
- `rag_engine/retriever_unified.py` (Qdrant retrieval)
- `rag_engine/prompt_builder.py`, `rag_engine/persona_loader.py` (system prompt assembly)
- `rag_engine/vantage_engine.py` (deterministic decision overlay)
- `rag_engine/gravity.py`, `rag_engine/vb_desire_profile.py`, `rag_engine/vb_tagging.py` (personalization signals)

## 1. System classification (“what kind of RAG is this?”)

**Current system is:**
- **Dense-vector RAG** using OpenAI embeddings + Qdrant vector search
- **Two-source retrieval**: personal episodic memory + corpus collections
- **Single-pass**: retrieve once, assemble prompt once, call model once
- **Personalized scoring**: feedback/tag/gravity/vb_desire biasing
- **Light adaptive routing**: greeting bypass, dynamic k split, thresholds, optional thread context

**Not currently implemented (in these modules):**
- No BM25/sparse retriever → not “hybrid retrieval” (dense+sparse fusion)
- No query rewriting/decomposition/multi-query → no “query transformation RAG”
- No multi-agent tool orchestration → not “agentic RAG” in the usual sense

## 2. Data stores & external dependencies

### 2.1 Qdrant
- `memory_raw`:
  - User episodic memory and memory cards (persona/style/preferences)
  - Also stores daemon cards: `gravity_profile`, `vb_desire_profile` (excluded from episodic retrieval)
- Corpus collections:
  - All Qdrant collections except `memory_raw` are treated as corpus by `unified_retrieve()`

### 2.2 Postgres
Used only in `vantage_router.py` (today):
- `chat_log` table for optional thread context (`mix.conversation`)
- `public.vantage_answer_trace` for durable answer attribution

### 2.3 Local HTTP services
- `http://127.0.0.1:8088/memory_feedback` (feedback signals for memory points)
- `http://127.0.0.1:8088/temporal/{user_id}` (time-since-last-message bucket)

### 2.4 OpenAI
- Embeddings model: `EMBED_MODEL` (default `text-embedding-3-large`)
- Chat model: defaults:
  - RAG: `gpt-5.2` (hardcoded in `rag_router.py`)
  - Vantage: `VANTAGE_MODEL` env or request override, default `gpt-5.2`

## 3. API entrypoints

### 3.1 `/rag/query` — Baseline RAG
File: `rag_engine/rag_router.py`

**Inputs**: `user_id`, `message`, optional `thread_id`, `top_k`, optional `overlay`

**Pipeline**:
1. Build temporary role overlay text:
   - `overlay_to_instructions(payload.overlay)` (optional)

2. Greeting bypass:
   - If `is_pure_reentry_greeting(message)`:
     - Build minimal system prompt (no memory injection)
     - Optional temporal re-entry prefix
     - Single model call → return

3. Retrieve personal memory:
   - `retrieve_personal_memory(user_id, message, top_k=min(8, top_k))`
   - Pulls from Qdrant `memory_raw`
   - Filters out assistant/system/daemon sources
   - Dedupe & removes obvious “prompty / probe” content

4. Personal memory rerank:
   - Load vb desire profile:
     - `load_latest_vb_desire_profile(user_id)` + `vb_desire_bias_map(card)`
   - Rerank with `score_personal_hit()` (rag_router version):
     - base vector score
     - feedback net (pos-neg)
     - simple format tag bonus
     - vb_desire tag bias
   - Keep top: `min(3, top_k)`

5. Retrieve corpus memory:
   - `unified_retrieve(message, top_k=top_k)`
   - Enumerates all Qdrant collections except `memory_raw`
   - Vector search per collection; merges into a global top-k
   - Adds small tag-based score bonuses (format/tone/intent match)

6. Prompt assembly:
   - `build_system_prompt(user_id, memory_chunks, overlay_text=overlay_text)`
   - Includes:
     - Persona block from Qdrant memory cards (`persona_loader.py`)
     - Temporary overlay text
     - Memory chunks formatted into a compact bullet list (`prompt_builder.py`)

7. Meta explanation:
   - `build_meta_explanation(user_id, message, memory_chunks)`:
     - inferred query tags
     - feedback summary
     - historical format consistency
     - gravity misalignment label
     - temporal bucket from `/temporal/{user_id}`

8. Optional gravity note:
   - If misalignment >= 0.4, append `[gravity-note]` into system prompt

9. Model call:
   - `complete_chat(system_prompt, message, model="gpt-5.2")`

10. Store last answer + personal memory ids (for feedback):
    - `_last_rag_result[(user_id, thread_id)] = {answer, memory_ids}`

**Outputs**:
- `answer`
- `memory_used` (combined personal + corpus)
- `system_prompt` (debug-style)
- `meta_explanation`

### 3.2 `/rag/feedback` — Feedback loop
File: `rag_engine/rag_router.py`

1. Identify last answer (keyed by user_id/thread_id)
2. Classify sentiment:
   - Deterministic markers first
   - Fallback: OpenAI “one-token” classifier
3. Optional tag extraction: `extract_tag_from_message()`
4. For each last-used personal memory id:
   - POST to `/memory_feedback`
5. Refresh persona:
   - `quick_persona_refresh(user_id)`

### 3.3 `/vantage/query` — Vantage endpoint (RAG + policy overlay + mix controls)
File: `rag_engine/vantage_router.py`

**Inputs**: `user_id`, `message`, optional `thread_id`, `top_k`, optional `overlay`, optional `limits`, optional `routing`, optional `mix`, optional `vantage_id`, optional `model`, optional `inspect_only`

**Major additions vs /rag/query**:
- Deterministic “Vantage Engine” overlay:
  - `extract_sd_features()` → `derive_params()` → `decide()`
  - Produces `response_class ∈ {COMPLY, NEGOTIATE, REFUSE, CLARIFY, REDIRECT}`
  - Produces response budgets (token_target, hedge_budget, etc.)
  - Builds an injected SYSTEM overlay text with rules

- `mix` controls:
  - `mix.memory_cards` and `mix.corpus` control the split between personal memory and corpus retrieval
  - `mix.similarity_threshold` sets score thresholds
  - `mix.recency_bias` adds score bonus for recent content (created_at/updated_at exponential decay)
  - `mix.conversation` enables thread-context injection from Postgres `chat_log`
  - `mix.lens_fm` injects a temporary “FM lens” constraint block

**Pipeline**:
1. Build overlay text:
   - user overlay: `overlay_to_instructions(payload.overlay)` (optional)
   - vantage overlay: `build_overlay_text(sd, limits, params, decision)`
   - optional thread context from Postgres: `_fetch_thread_context_block()`
   - optional FM lens block

2. (Feature note) Greeting bypass exists but does **not** return early in current code:
   - The `is_pure_reentry_greeting()` block sets local variables but does not short-circuit.
   - As written, it appears ineffective and should be treated as a bug or unfinished behavior.

3. Compute k split:
   - `k_personal = base_k * mix.memory_cards`
   - `k_corpus   = base_k * mix.corpus`
   - If `VANTAGE_PERSONAL_MEMORY != 1` or weight is 0, personal memory retrieval is disabled.

4. Retrieve personal memory:
   - `retrieve_personal_memory(user_id, message, top_k=min(8, k_personal), threshold, vantage_id)`
   - Rerank with `score_personal_hit()` imported from `rag_router.py` (explicitly to match current prod behavior)
   - Keep top `min(3, top_k)`
   - Apply recency bias after rerank

5. Retrieve corpus memory:
   - `unified_retrieve(message, top_k=k_corpus, score_threshold=...)`
   - Apply recency bias

6. Prompt assembly:
   - `build_system_prompt(... include_persona=False)` (no persona block)

7. Optional reentry prefix (temporal_policy) unless response_class == CLARIFY

8. `inspect_only`:
   - Return retrieval + prompt without model call

9. Model call:
   - `complete_chat(system_prompt, message, model=model_id)`
   - If response_class == CLARIFY, enforce “questions only” shape with `_enforce_clarify_shape()`

10. Write attribution trace:
   - Insert into `public.vantage_answer_trace`

### 3.4 `/vantage/feedback`
Similar to `/rag/feedback`, but:
- Supports `answer_id` lookup in `public.vantage_answer_trace`
- Always triggers `quick_persona_refresh(user_id)`

## 4. Retrieval details

### 4.1 Query tagging
- `infer_query_tags(text)` in `retriever_unified.py`:
  - format: skeleton/prose
  - tone: meta
  - topic: workout/fm/hv (hardcoded heuristics)
  - intent: explain/instruct/summarize/analyze/compare/reflect/generate/rewrite/evaluate
  - plus VB tags from `vb_tagging.infer_vb_tags()`

Tags are used for:
- Personal memory reranking nudges
- Corpus hit score bonuses
- Meta explanation
- Gravity misalignment label

### 4.2 Corpus retrieval (`unified_retrieve`)
- Embeds the query (OpenAI embeddings)
- Lists all Qdrant collections dynamically (excluding `memory_raw`)
- Runs vector search per collection
- Adds tag bonus to each hit’s score
- Returns global top-k across all collections

### 4.3 Personal memory retrieval (`retrieve_personal_memory`)
- Embeds the query
- Filters `memory_raw` by:
  - user_id
  - optionally `vantage_id` namespace (match or missing payload.vantage_id)
  - excludes sources:
    - `frontend/chat:assistant`
    - `gravity_daemon`
    - `vb_desire_daemon`
- Dedupe and filter out “prompty/test” memories unless query is itself a test
- Adds tag-based nudges and gravity-based bonus/penalty
- Returns top_k results

### 4.4 Feedback loops
- Feedback signals are used in reranking (`score_personal_hit`)
- Memory feedback updates are sent to `/memory_feedback`
- Persona refresh synthesizes/updates a style card based on recent messages

## 5. Prompt assembly

### 5.1 `prompt_builder.build_system_prompt()`
Pieces, in order:
1. Persona block (optional; enabled for `/rag/query`, disabled for `/vantage/query`)
2. Temporary overlay text (role overlay + vantage overlay + optional thread context)
3. Memory block:
   - Formatted as a bullet list with provenance tags `[collection][kind]`
   - Dedupes identical content across collections

## 6. Environment variables

Common:
- `OPENAI_API_KEY`
- `EMBED_MODEL` (default `text-embedding-3-large`)
- `QDRANT_URL` (default `http://127.0.0.1:6333`)

Retrieval:
- `RETRIEVE_THRESHOLD` (default 0.30 in `unified_retrieve()`)

Vantage endpoint:
- `ENABLE_VANTAGE_ENDPOINTS` must be `"1"` or endpoint returns 404
- `VANTAGE_DEBUG` (enables extra meta fields)
- `VANTAGE_PERSONAL_MEMORY` (enables personal memory retrieval)
- `VANTAGE_MODEL` (default model id for vantage)
- `POSTGRES_DSN` (for thread context + answer trace)

Feedback classifier:
- `FEEDBACK_MODEL` (default `gpt-4o-mini`)

## 7. Known legacy / code smells (actionable)

1. `vantage_router.py` greeting bypass appears ineffective:
   - It does not return early, and later code overwrites `system_prompt` and `meta`.

2. `vb_desire_profile.py` contains duplicated function definitions:
   - `load_latest_vb_desire_profile()` and `vb_desire_bias_map()` are defined twice; the second definition wins.

3. Two “retriever” modules exist:
   - `retriever_unified.py` is used by routers.
   - `retriever.py` looks legacy (fixed collection list + raw HTTP to Qdrant) and may be unused.

4. Retrieval is dense-only:
   - There is no BM25/sparse retriever or fusion strategy (RRF/weighted merge).

## 8. Improvement roadmap (high-level)

See `docs/improvements.md` (to be created) for prioritized work items:
- Observability & tracing
- Retrieval upgrades (rerank, hybrid, query transformation)
- Prompt/context budgeting
- Consolidation of duplicated logic
- Integration of structured stores (`vantage_card`, `vantage_fact`) if desired
