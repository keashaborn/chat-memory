# Vantage Engine — Docs Index

This repo has two Vantage docs with different roles:

- **As-built (source of truth for what is running):** `docs/vantage_controls.md`
- **Design/spec snapshot (historical planning doc):** `docs/vantage_engine_spec_v0.2.md`

If these ever disagree, treat **vantage_controls.md** as authoritative for runtime behavior.

---

## Quick runtime reference (current)

**Servers**
- **seebx (backend)**: Brains + `rag_engine` → `http://127.0.0.1:8088`
- **Verbal Sage (frontend)**: Next.js UI → `http://127.0.0.1:3010` (only on the Verbal Sage box)

**Endpoints**
- Brains: `POST /vantage/query`, `POST /vantage/feedback`
- Verbal Sage: `POST /api/chat`, `POST /api/chat/feedback`, `POST /api/tts`

**Controls**
- UI writes cookie: `vs_vantage_limits` with JSON `{ "Y":0..1, "R":0..1, "C":0..1, "S":0..1 }`
- Frontend forwards limits to Brains (see `docs/vantage_controls.md`)

**Debug**
- With debug enabled, Brains returns: `meta_explanation.vantage.{sd,limits,params,decision}`

---

## Maintenance rule

- If code behavior changes: update **`docs/vantage_controls.md`**
- If future plans change: update **`docs/vantage_engine_spec_v0.2.md`** (or create new spec docs)
