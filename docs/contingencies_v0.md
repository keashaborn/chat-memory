# Brains / Verbal Sage — Contingencies Map v0

**File:** /opt/chat-memory/docs/contingencies_v0.md  
**Scope:** backend “Brains” FastAPI + rag_engine (memory + RAG + reinforcement).  
**Non-scope:** frontend UI, RESSE corpus builder, training.

This document is an audit/handoff artifact. It does not affect runtime. It exists to map:
- What endpoints exist.
- What data is “state” vs raw transcript.
- What currently functions as reinforcement / learning.
- Where future persona/governor work should hook in with minimal churn.

---

## Runtime topology

- **Brains FastAPI service:** `brains.service` → uvicorn serving `app.py` on `:8088`
- **Qdrant:** `http://127.0.0.1:6333`
- **Postgres:** DSN via `POSTGRES_DSN` env var (fallback exists in code)

---

## Data stores

### Postgres (authoritative transcript)
- Table: `chat_log`
  - columns (as used by app.py): `id (uuid)`, `user_id (text)`, `source (text)`, `text (text)`, `tags (text[])`, `thread_id (uuid)`, `created_at (timestamptz default now())`
- Table: `threads`
  - used by `/threads/*` endpoints and referenced by `/log`
  - **Note:** schema is assumed to exist; `/log` does not CREATE TABLE threads.

### Qdrant (vector memory + cards + corpus)
- Collection: `memory_raw`
  - holds:
    - episodic chat points (user + assistant)
    - memory cards (source=`memory_card`)
    - daemon cards (gravity/vb_desire)
  - payload conventions (observed):
    - `text`, `user_id`, `source`, `tags`, optional `thread_id`
    - optional `feedback` dict with counters
    - optional `user_tags` list for explicit tagging
    - optional card fields: `kind`, `base_importance`, `created_at`, `updated_at`
- Corpus collections (dynamic)
  - all Qdrant collections except `memory_raw` are treated as corpus sources by `rag_engine.retriever_unified.list_collections()` / `unified_retrieve()`.

---

## Endpoint inventory

### RAG endpoints (rag_engine/rag_router.py)
- `POST /rag/query`
  - main answer path (RAG + persona + overlay + temporal re-entry)
- `POST /rag/feedback`
  - interprets user text as feedback on last answer and dispatches reinforcement to Brains `/memory_feedback`

### Brains endpoints (app.py)
- `POST /log`
  - writes Postgres transcript row and best-effort upserts a matching `memory_raw` point
- `POST /memory_feedback`
  - updates feedback counters + optional `user_tags` on a specific `memory_raw` point
- `GET /temporal/{user_id}`
  - seconds since last user message (from Postgres), bucketed
- `POST /gravity/rebuild`
  - recompute and write a `gravity_profile` card
- `POST /vb_desire/rebuild`
  - recompute and write a `vb_desire_profile` card
- Retrieval/debug endpoints:
  - `POST /retrieve` (corpus collections)
  - `POST /retrieve_memory` (memory_raw with optional user_id filter)
- Threads:
  - `POST /threads/new`
  - `GET /threads/list/{user_id}`
  - `GET /threads/{thread_id}/messages`
  - `POST /threads/{thread_id}/rename`
  - `POST /threads/{thread_id}/archive`
  - `DELETE /threads/{thread_id}`
- Cards:
  - `GET /cards/{user_id}`
  - `DELETE /cards/{user_id}/{card_id}`
- Privacy/admin:
  - `DELETE /user/{user_id}/data`
  - `DELETE /user/{user_id}/recent`
  - `GET /user/{user_id}/export`
- Health:
  - `GET /healthz`
  - `GET /openapi.json`

---

## Current “learning state” artifacts (Qdrant memory_raw cards)

Observed for real user `1240822d-ac9a-4096-95aa-e2b24d36ef50`:
- `kind=user_identity` (source=`memory_card`, deterministic uuid5 id)
- `kind=style` (source=`memory_card`, deterministic uuid5 id via persona_loader.quick_persona_refresh)
- `kind=gravity_profile` (source=`gravity_daemon`, new uuid each rebuild)
- `kind=vb_desire_profile` (source=`vb_desire_daemon`, new uuid each rebuild)

Other kinds exist in codepaths but may not exist yet in data:
- `assistant_identity`, `preference`, `style_mode` (persona_loader expects these kinds)
- `persona_profile`, `preference_profile` appear in some endpoint defaults but are not currently produced by a daemon in the audited paths.

---

## Primary request paths

### A) Transcript logging: frontend → `POST /log` (app.py)
Behavior:
1) Parse `{text, user_id, source, tags, optional thread_id}`.
2) Add heuristic tags via `infer_extra_tags(text, source=source)` (format/topic/intent + vb tagging).
3) Write to Postgres `chat_log`.
4) Best-effort embed and upsert into Qdrant `memory_raw`.

Important implementation notes (from audit work):
- A **single stable `rec_id`** is generated and used as both:
  - Postgres `chat_log.id`
  - Qdrant `memory_raw` point id  
  This fixes prior “id mismatch” issues and makes deletion/export coherent.
- `/log` now returns a JSON body: `{"status":"ok","id": rec_id}` (previously returned `null` due to missing return).
- If `OPENAI_API_KEY` is missing, Qdrant upsert is skipped; Postgres transcript remains authoritative.

### B) Answer generation: frontend → `POST /rag/query` (rag_router.py)
High-level flow:
1) Overlay: optional `overlay` object → `role_overlay.overlay_to_instructions()` (TEMPORARY, not stored).
2) “Pure re-entry greeting” short-circuit:
   - `is_pure_reentry_greeting()` bypasses retrieval and uses a minimal system prompt to avoid injecting memory.
3) Personal memory retrieval:
   - `retrieve_personal_memory(user_id, query)` from `retriever_unified.py`
   - must_not filters exclude:
     - assistant chat points (`source=frontend/chat:assistant`)
     - cards (`source=memory_card`)
     - daemon points (`source=gravity_daemon`, `source=vb_desire_daemon`)
4) Personal memory rerank (second stage, inside rag_router.py):
   - `rag_router.score_personal_hit()` = base score + feedback bonus + small format bonus + vb_desire bias-map
5) Corpus retrieval:
   - `unified_retrieve()` searches all Qdrant collections except `memory_raw`
6) Build prompt:
   - `prompt_builder.build_system_prompt()`:
     - persona block: `persona_loader.build_persona_block(user_id)`
     - overlay instructions (if provided)
     - “Relevant context from memory” (formatted memory chunks)
7) Meta reasoning (returned to caller):
   - `build_meta_explanation()` includes:
     - inferred query_tags
     - feedback summary over used memories
     - topic tags summary
     - historical format leaning vs current request
     - gravity misalignment label + numeric score
     - temporal info from `GET /temporal/{user_id}`
8) Temporal re-entry prefix:
   - `temporal_policy.should_add_reentry_line()` / `build_reentry_line()` may prepend a re-entry line.
9) Gravity “escape hint”:
   - If misalignment >= 0.4, rag_router injects a `[gravity-note]` into the system prompt:
     “prioritize explicit request even if it differs from past patterns”
10) Generate answer:
   - `complete_chat(system_prompt, payload.message, model=model_id)` where `model_id` is chosen in `rag_router.py` (request/env). `rag_engine/openai_client.py` default is `gpt-5.1`.
11) Store last personal memory ids for reinforcement:
   - `_last_rag_result[user_id] = {answer, memory_ids}`

### C) Reinforcement: frontend → `POST /rag/feedback` (rag_router.py)
Behavior:
1) Pull last answer + memory_ids from in-process `_last_rag_result[user_id]`.
2) Classify sentiment of user feedback:
   - `classify_feedback_nl(last_answer, user_message)`:
     - regex/substring markers first
     - fallback: OpenAI classification (model from `FEEDBACK_MODEL`, default `gpt-4o-mini`)
3) Extract explicit tag:
   - `extract_tag_from_message()` parses “tag this as …” → slug
4) Dispatch to Brains:
   - POST `/memory_feedback` for each memory_id with `{user_id, memory_id, signal, optional tag}`
5) Trigger implicit preference extraction:
   - `quick_persona_refresh(user_id)` runs after feedback dispatch.

### D) Reinforcement storage: `POST /memory_feedback` (app.py)
Behavior:
- Retrieve `memory_raw` point by id.
- Verify `payload.user_id` matches.
- Increment:
  - `payload.feedback.positive_signals` or `.negative_signals`
  - update `payload.feedback.last_feedback_at`
- If tag provided:
  - append to `payload.user_tags[]` (dedup)
- Upsert updated point.

---

## Preference extraction loop (implicit, currently small)

Trigger:
- Called from `/rag/feedback` as a side-effect: `persona_loader.quick_persona_refresh(user_id)`.

Mechanism (persona_loader.py):
- Scroll recent `memory_raw` points for the user (default limit=100).
- Detect crude style preference markers:
  - “too long” / “shorter” → “Prefers short, dense responses.”
  - “no bullet” / “no lists” → “Dislikes bullet points…”
  - “more concrete” → “Prefers concrete examples…”
- Write/overwrite deterministic style card:
  - `kind=style`, `source=memory_card`, tags `["summary","card","style"]`
  - deterministic uuid5: `{user_id}-style-card`

Operational gotcha (from audit work):
- Running `persona_loader.quick_persona_refresh` manually requires:
  - using venv python (`/opt/chat-memory/venv/bin/python3`)
  - env vars loaded (`. /opt/chat-memory/.env`)
  Otherwise you can hit `ModuleNotFoundError: qdrant_client` or missing OpenAI key.

---

## Tagging pipeline (what “features” exist today)

### Heuristic tags (format/topic/intent) for stored memories
- `app.py: infer_extra_tags(text, source)` adds:
  - `format:*`, `tone:*`, `topic:*`, `intent:*`
  - plus VB tags from `rag_engine/vb_tagging.infer_vb_tags(text, source=source)`

### Query tags (used at retrieval time)
- `retriever_unified.infer_query_tags(text)`:
  - similar heuristics for format/tone/topic/intent
  - calls `infer_vb_tags(text)` **with default source="user"**

### Explicit user tags
- Added by `/rag/feedback` parsing “tag this as …”
- Stored on memory points as `payload.user_tags[]`
- Used by scoring/reranking.

### Feedback signals
- Stored on memory points as `payload.feedback.{positive_signals, negative_signals, last_feedback_at}`
- Used by reranking in multiple places.

---

## What gets reinforced (current scope)
Only personal-memory points used by `/rag/query` get reinforced:
- IDs come from `retrieve_personal_memory()` results (user episodic, not cards, not assistant, not daemons).
- Corpus memories are not reinforced.

---

## Prompt injection points
- `prompt_builder.build_system_prompt()`:
  - persona block: `persona_loader.build_persona_block(user_id)`
  - overlay text (temporary): `role_overlay.overlay_to_instructions()`
  - formatted memory chunks under “Relevant context from memory: …”

Persona composition (persona_loader.py):
- BASE_PERSONA (static)
- cards pulled from `memory_raw` where `source=memory_card`:
  - `kind=user_identity` (max 1)
  - `kind=assistant_identity` (max 1)
  - `kind=style` (max 3)
  - `kind=style_mode` (max 3)
  - `kind=preference` (max 5)

---

## Known mismatches / issues / gotchas

### VB tagging source normalization (fixed behind flag)
- `rag_engine/vb_tagging.infer_vb_tags(text, source=...)` strips `vb_desire:*` and `vb_fiction:*` unless `source == "user"`.
- Brains `/log` receives sources like `"frontend/chat:user"` and `"frontend/chat:assistant"`.
- Fix implemented in `app.py` behind an env flag:
  - `VB_TAG_SOURCE_NORMALIZE=1` normalizes the VB tagging source before calling `infer_vb_tags()`:
    - `"*chat:user"` → `"user"`
    - `"*chat:assistant"` → `"assistant"`
  - When unset/`0` (default), behavior is unchanged.
- Verification (audit):
  - With flag unset, stored tags for `"frontend/chat:user"` include `vb_relation:*` but not `vb_desire/vb_fiction`.
  - With flag set, new `/log` writes include `vb_desire:explicit_request` and `vb_fiction:mentalistic_term` when applicable.
- Note: this affects **new** points only; existing `memory_raw` points are not backfilled.



### Gravity v0 notes (rag_engine/gravity.py)
- `write_gravity_card()` uses a new uuid each rebuild → multiple gravity_profile cards accumulate.
- `load_gravity_profile()` selects “latest” as `points[-1]` from Qdrant scroll; scroll order is not guaranteed chronological.
- `load_user_memories()` reads only a single scroll page with `limit=20000`; if user exceeds this, gravity truncates silently.
- `compute_misalignment()` is sign-based on overlapping tags; “no overlap” returns 0.3 (mild) by design.

### Duplicate functions / divergence risk (from dupe scan)
- `score_personal_hit` exists in:
  - `rag_engine/rag_router.py` (used for reranking after `retrieve_personal_memory`)
  - `rag_engine/retriever_unified.py` (not used in the audited `/rag/query` path; verify with grep before deleting)
  Risk: tuning one without the other causes drift.
- `get_qdrant` exists in:
  - `app.py` (Brains service singleton)
  - `rag_engine/persona_loader.py` (persona loader singleton)
  Risk: config drift; consider centralizing later.
- Same-file duplicates (likely accidental; fix when cleaning):
  - `rag_engine/vb_desire_profile.py`: `load_latest_vb_desire_profile` defined twice
  - `rag_engine/vb_desire_profile.py`: `vb_desire_bias_map` defined twice


### Qdrant client/server version mismatch (ad-hoc scripts)
- Ad-hoc python scripts may warn if client/server minor versions differ by >1.
- Observed warning: client `qdrant_client==1.15.1` vs server `1.11.0`.
- Code-side clients often set `check_compatibility=False`; do the same in manual scripts if needed.

---

## Audit actions performed (session notes)
- Implemented `VB_TAG_SOURCE_NORMALIZE=1` (Brains `app.py`) to preserve `vb_desire:*` and `vb_fiction:*` tags for `source="frontend/chat:user"`.
- Verified via `/log` + Qdrant retrieve that new stored points include `vb_desire:explicit_request` and `vb_fiction:mentalistic_term` when applicable.
- Fixed `/log` to return a JSON body (was returning `null`).
- Fixed `/log` so Postgres `chat_log.id` and Qdrant `memory_raw` point id use the same `rec_id`.
- DSN is read from `POSTGRES_DSN` env var (with fallback).
- Verified cards for real user:
  - `user_identity`, multiple `gravity_profile`, one `vb_desire_profile`
  - created `style` card via `quick_persona_refresh` once env+venv were used correctly.
- Confirmed that stored user points carry `vb_relation:*` etc but **not** `vb_desire:*` due to source mismatch.

---

## Minimal handoff notes for the “new Governor/Renderer system” (future work)
Do NOT implement now; this is just to preserve where it should plug in.

Best hook points (low churn):
- In `rag_engine/rag_router.py` between:
  - after `memory_chunks` are assembled
  - before `build_system_prompt()` / `complete_chat()`
- Candidate place to introduce a Governor decision object:
  - using existing features: `infer_query_tags`, gravity weights, vb_desire_profile, feedback summaries, temporal bucket.
- Candidate place to store persona state:
  - new `memory_raw` card kind (e.g., `persona_policy` / `persona_state`) with deterministic id per `(user_id, persona_id)`.

Compatibility requirement (strongly suggested):
- Add `persona_id` to:
  - `/log` payload and Qdrant memory payload
  - `/rag/query` request schema
  - personal-memory retrieval filter and reinforcement routing
  so multiple personas can learn independently without cross-contamination.

---

## Config surface (env vars)
These are the runtime knobs currently referenced in the audited codepaths.

- `POSTGRES_DSN` — Postgres DSN used by `app.py` (chat_log + threads + temporal).
- `OPENAI_API_KEY` — required for embeddings (Qdrant upserts, cards) and completions.
- `QDRANT_URL` — Qdrant endpoint (used across app.py + rag_engine modules).
- `EMBED_MODEL` — embedding model (default: `text-embedding-3-large`).
- `VB_TAG_SOURCE_NORMALIZE` — if `1`, normalize `source` to `user`/`assistant` before `infer_vb_tags` so stored user points keep `vb_desire:*` and `vb_fiction:*` (default: `0`).
- `RETRIEVAL_COLLECTION` — legacy default shown in `/healthz`; current RAG retrieval uses “all collections except memory_raw”.
- `RETRIEVE_TOP_K` — fallback top_k for retrieval endpoints.
- `RETRIEVE_THRESHOLD` — fallback score threshold for retrieval endpoints.
- `FEEDBACK_MODEL` — model used by `/rag/feedback` classifier (default: `gpt-4o-mini`).


## Ops runbook (seebx)
Service + debugging commands that should be safe during audits.

- Syntax check:
  - `/opt/chat-memory/venv/bin/python3 -m py_compile /opt/chat-memory/app.py`
- Restart Brains:
  - `sudo systemctl restart brains`
  - `sudo systemctl status brains --no-pager -l | sed -n '1,25p'`
- Logs:
  - `tail -n 200 /opt/chat-memory/logs/service.log`
- Smoke checks:
  - `curl -sS http://127.0.0.1:8088/healthz`
  - `curl -sS -i -X POST http://127.0.0.1:8088/log -H 'content-type: application/json' -d '{"text":"audit","user_id":"<uid>","source":"frontend/chat:user","tags":["audit"]}'`
  - `curl -sS "http://127.0.0.1:8088/cards/<uid>?limit=10"`
- Manual scripts MUST use venv + env:
  - `set -a; . /opt/chat-memory/.env; set +a`
  - `/opt/chat-memory/venv/bin/python3 - <<'PY' ... PY`


## Cleanup backlog (deferred; do not change during audit unless necessary)
These are the “make it clean” items that reduce long-term drift, but can be postponed until after audit stability.

1) `rag_engine/vb_desire_profile.py`: remove same-file duplicate defs (`load_latest_vb_desire_profile`, `vb_desire_bias_map`) and add a tiny unit test.
2) `rag_engine/gravity.py`: select latest `gravity_profile` by `updated_at/created_at`, not scroll order; paginate scroll beyond 20k.
3) VB tagging normalization: decide whether to keep `VB_TAG_SOURCE_NORMALIZE=1` enabled permanently; optional backfill/migration for older `memory_raw` points that were logged before the flag existed.
4) Consolidate or explicitly differentiate `score_personal_hit` (single implementation vs two-stage with documented deltas).
5) Centralize `get_qdrant()` construction (or enforce shared helper) to avoid configuration drift.
6) Align Qdrant client/server versions (observed: client 1.15.1, server 1.11.0) to remove compatibility warnings.
7) Consider asyncpg pooling for hot endpoints (`/log`, `/temporal`, `/threads/*`) if latency becomes an issue.


## Governor/Renderer v1 interface contract (draft)
This is guidance for the “new system” chat; do not implement here yet.

Low-churn insertion point (recommended):
- `rag_engine/rag_router.py` after personal+corpus `memory_chunks` are assembled and before:
  - `prompt_builder.build_system_prompt()`
  - `openai_client.complete_chat()`

Existing signals you can reuse immediately as SD_features:
- `infer_query_tags(query)` output
- gravity weights + `compute_misalignment()`
- `vb_desire_profile` (bias-map)
- feedback summary over last-used memory ids (pos/neg counters + user_tags)
- temporal bucket from `GET /temporal/{user_id}`

Proposed Governor output (pure data, no prompt text):
- `{ action: comply|negotiate|refuse, stance_change_allowed: bool, required_concessions: [...], tone_directives: {...}, output_format: {...} }`

Renderer:
- Converts Governor output into temporary “overlay_text” (do NOT store) using the already-existing overlay injection path:
  - `role_overlay.overlay_to_instructions()` + `prompt_builder.build_system_prompt()`

Persona isolation requirement:
- Add `persona_id` to:
  - `/log` request + Qdrant payload
  - `/rag/query` request
  - personal-memory retrieval filters + reinforcement routing
to prevent cross-contamination when multiple personas exist.
