# Vantage System Audit — 2026-01-02

## Topology (current)
- Frontend (Verbal Sage / Next.js): `verbalsage-v2.service` on 127.0.0.1:3010
- Backend (seebx / Brains FastAPI): `brains.service` on 0.0.0.0:8088
- Qdrant: memory + corpus collections
- Postgres: transcripts + identity tables (vantage_identity schema)

## Changes landed in this session

### Frontend
- Added support for `vs_vantage_roleplay` cookie (currently used as a “characterization overlay” input).
- `/api/chat` and `/api/chat/inspect` now accept roleplay in body and from cookie, sanitize it, and forward it downstream.
- Admin settings store persists `vs_vantage_roleplay` and includes roleplay in profile persistence.

### Backend (rag_engine/vantage_router.py)
- Pragmatics parse now includes `df` in addition to `rfg` and `pe`.
- Roleplay overlay block added (prompt-only overlay; no longer forces a user-visible prefix).
- Removed forced user-visible disclosure prefix (“Role-play mode: ON.”) and prompt line enforcing it.
- Phatic bypass follow-up changed from “What do you want to work on?” → “What’s on your mind?”

## Current “dial” wiring status (RFG / DF / PE)
- `rfg`: actively used (controls phatic ritual bypass).
- `pe`: actively used (selects phatic base phrasing).
- `df`: parsed and currently used only inside the overlay logic (not yet a general renderer control).

## Known mismatches / issues vs lab direction

1) Personal memory scoping is not guaranteed end-to-end yet
- `retrieve_personal_memory()` supports `vantage_id` filtering.
- Need to confirm `vantage_router.py` passes `vantage_id` into the personal memory call mapping (it currently passes it into corpus retrieval; personal mapping may still be missing it).

2) persona_loader is user-only
- `rag_engine/persona_loader.py` filters memory_card points by `user_id` only.
- Lab direction requires scoping by `vantage_id` + `scope` (and optionally `actor_id` for relationship scope).

3) Corpus collection allowlist/denylist not implemented
- `unified_retrieve()` currently searches all Qdrant collections except memory_raw.
- Before introducing internal collections (policy, prototypes, organism state), we must add env-driven corpus filtering.

4) Deterministic phatic bypass exists
- Even with rfg as a dial, this is still pre-canned surface behavior.
- In lab mode, default should be “no deterministic bypass” (or gate behind an env flag).

5) Terminology debt
- “roleplay” is currently the name of the overlay feature in UI/cookies/back-end schema.
- Lab framing should rename this to “vantage definition / characterization / identity overlay” and remove roleplay framing.

## Next implementation priorities (ordered for low-churn)
A) Enforce corpus allowlist/denylist in retriever_unified.list_collections().
B) Ensure `vantage_id` is passed into personal memory retrieval from vantage_router.
C) Introduce `actor_id` + `scope` fields for memory points + logs (initially actor_id=user_id for single-user).
D) Update persona_loader and future identity cards to filter by vantage_id (+ scope), not user-only.
E) Replace “roleplay” naming with lab terminology across frontend/backend.
F) Disable deterministic phatic bypass by default (env gate), so “canned greetings” are not the default organism behavior.
