# Vantage Memory Next Actions

This document is the short, working checklist. Update it frequently. Keep the implementation log append-only. Keep the original audit/source document mostly stable.

---

## Immediate Next Target

### 1. Confirm live prompt path uses Vantage-scoped preference cards

The pipeline now creates RESSE preference cards from natural speech:

```text
pref/format: no_bullet_points
pref/response_length: concise
pref/specificity: less_generic
```

Next question:

```text
Are these cards actually injected into the live prompt/persona path?
```

Inspect likely code paths:

```text
rag_engine/persona_loader.py
rag_engine/prompt_builder.py
rag_engine/retriever_unified.py
rag_engine/vantage_router.py
app.py
```

Desired behavior:

```text
User chats with RESSE
  -> system retrieves RESSE-scoped preference cards
  -> prompt includes concise/no-bullets/less-generic preferences
  -> response adapts
```

Test should verify:

```text
RESSE retrieves RESSE cards.
EVA does not inherit RESSE cards unless explicitly configured.
Legacy default/no-vantage cards do not pollute non-default Vantages.
```

---

## Open Architecture Items

### 2. Personalization appears user-global, not Vantage-specific

Observed when creating EVA:

```text
Personalization/instructions entered for one Vantage appeared when viewing another.
```

Likely current behavior:

```text
personalization = user-global
```

Desired future behavior:

```text
base user personalization = user-global
Vantage-specific instructions = stored with the Vantage profile
Vantage-specific role/persona overlay = stored with the Vantage profile
```

Do not fix until after prompt-card injection is verified.

---

### 3. Improve natural-language feedback extraction

Current extractor is deterministic and conservative.

Current detected patterns include:

```text
concise / short responses
avoid long responses
no bullet points
prose over bullets
avoid numbered answers
direct / straightforward style
less generic / more specific
positive feedback
```

Future improvements:

```text
Capture "too much explanation."
Capture "more technical."
Capture "less mystical."
Capture "more playful."
Capture "warmer / colder."
Capture "more clinical."
Capture "more poetic."
Capture "ask fewer questions."
Capture "answer first."
Separate temporary state from long-term preference.
```

Potential later architecture:

```text
deterministic extractor first
LLM extractor second
confidence / contradiction handling
human-visible audit of extracted preferences
```

---

### 4. Reconcile old default/null cards and facts

There are old cards and fact sources under:

```text
default
<NULL>
```

Need decide how to handle them.

Options:

```text
leave as legacy only
migrate selected ones to RESSE
quarantine old test/harness facts
deduplicate user aliases
```

Do not bulk migrate until the live prompt path is clear.

---

### 5. Memory Cards UI should become Vantage-first

Current issue:

```text
Memory Cards UI appears organized mostly around card kind.
```

Desired:

```text
select user/account
select Vantage
show cards for that Vantage
filter by kind
inspect evidence/source links
```

Useful UI sections:

```text
Vantage registry rows
active/default indicator
card counts by kind
recently updated cards
source/evidence trace
```

---

### 6. User identity / alias cleanup

Known issue:

```text
Some cards show different user ids / aliases.
```

Examples seen:

```text
1240822d-ac9a-4096-95aa-e2b24d36ef50
dcb8fb63-6613-4a09-8db2-7a879dc90146
```

Need clarify whether these are:

```text
real separate users
old aliases
legacy test users
```

Then decide how to canonicalize or migrate.

---

### 7. Confirm gravity and VB desire profile are Vantage-scoped

Known earlier diagnosis:

```text
gravity.py was user-global.
vb_desire_profile.py was user-global.
```

Next inspection should confirm whether they still ignore `vantage_id`.

Desired:

```text
gravity_profile per user_id + vantage_id
vb_desire_profile per user_id + vantage_id
```

Avoid cross-contamination between RESSE and EVA.

---

### 8. Confirm registry-driven daemon remains stable

Current systemd target:

```text
vantage-initiator.service -> --all-from-registry
vantage-initiator-resse.service -> disabled/stopped
```

Periodic check:

```bash
sudo systemctl status vantage-initiator.service --no-pager -l
tail -n 120 /opt/chat-memory/logs/initiator.log
```

Expected:

```text
registry mode: ticking vantages=['RESSE', 'EVA']
no Traceback
no failed jobs
```

---

## Current Known Good State

```text
Brains healthy.
Frontend healthy.
Vantage registry mirror live.
RESSE and EVA mirrored.
One registry-driven daemon running.
Natural-language feedback enters fact pipeline.
RESSE feedback cards created.
Fact extraction is Vantage-scoped.
Fact drives are Vantage-scoped.
Card consolidation is Vantage-scoped.
No-op EVA extraction patched.
```

---

## Next Session Starting Command

Backend health and registry:

```bash
cd /opt/chat-memory
curl -sS http://127.0.0.1:8088/readyz
curl -sS "http://127.0.0.1:8088/vantages/1240822d-ac9a-4096-95aa-e2b24d36ef50" | python3 -m json.tool
```

Daemon check:

```bash
sudo systemctl status vantage-initiator.service --no-pager -l | sed -n '1,35p'
tail -n 100 /opt/chat-memory/logs/initiator.log | grep -E "registry mode|failed|ERROR|Traceback" || true
```

Likely next task:

```text
Trace live prompt/persona loading and verify whether RESSE preference cards affect actual responses.
```

---

## Next Audit Targets After Live Vantage Memory Loop

1. Legacy memory audit: Qdrant memory_raw, legacy memory_card records, gravity_profile, vb_desire_profile, user_identity, style/style_mode/preference cards.

2. Evidence deduping: manual source reprocessing can duplicate evidence rows, especially doc.content_sha256.

3. Vantage lever audit: test FM lens strength, mix sliders, routing controls, pragmatics, roleplay/definition overlay, response length/token budget, clarify bias, and memory-card weighting.

4. Extractor expansion: broaden natural-language extraction gradually for likes/dislikes, career history, project state, current task state, stable style preferences, and avoidance/punishment patterns.

Working assumption: keep Qdrant vector memory for semantic retrieval, but do not build new behavior on legacy gravity/vb_desire unless intentionally migrated.

## 2026-06-16 — Roadmap and Vantage UI Reframing

### Completed Since Earlier Notes

- Vantage-specific personalization is now explicitly scoped by `vantage_id`.
- RESSE personalization no longer appears automatically under EVA.
- Personalization now opens as an embedded Settings drawer subview instead of a detached global-feeling page.
- Core Vantage levers were verified through the UI/inspect path:
  - FM lens
  - corpus retrieval
  - personal memory
  - similarity cutoff
  - S / ornament budget
  - clarify/routing
  - pragmatics/social presence
  - Y/R/C low-end mechanical pass-through
- Product architecture roadmap created:
  - `docs/verbal_sage_product_architecture_roadmap.md`

### Next Practical UI Target

Reframe the Vantage Profile UI away from "preset" language and toward durable Vantage language.

Preferred wording:

- "Load preset" -> "Active Vantage" or "Load Vantage"
- "Save preset" -> "Save Vantage"
- "Set default preset" -> "Set default Vantage"
- "Delete preset" -> "Archive/Delete Vantage"

Reason:

RESSE and EVA should feel like stable Vantages with their own identity, personalization, memory, and cards, not temporary slider presets.

### Do Not Build Yet

Do not immediately build:

- Character Studio
- multi-character rooms
- public character library
- image generation
- shared/collective character systems

Those are now captured in the roadmap and should be built after the Core Vantage model is stable.

## 2026-06-16 — Vantage Identity Model Audit

### Current Storage Model

The frontend Vantage Profile system currently stores Vantages as profile records with:

- `profile.id`
- `profile.name`
- `profile.state.vantageId`
- `profile.state.limits`
- `profile.state.routing`
- `profile.state.mix`
- `profile.state.pragmatics`
- `profile.state.roleplay`

Cloud source of truth:

- Supabase `user_metadata.vs_vantage_profiles_v1`

Local cache:

- `localStorage.vs_vantage_profiles`

Backend mirror:

- `/api/vantages/sync`
- Brains Vantage registry

Default Vantage is currently stored as:

- `defaultId = profile.id`

Runtime memory/prompt namespace uses:

- `state.vantageId`

### Risk

The model currently has three identifiers:

- internal profile ID
- display/profile name
- runtime `state.vantageId`

If `profile.name` and `state.vantageId` diverge, the UI may show one Vantage while memory, cards, and prompt injection use another namespace.

### Future Fix

Move toward an explicit durable Vantage model:

- `vantage_key` or `vantage_id`: stable immutable namespace, e.g. `RESSE`
- `display_name`: editable UI label, e.g. `RESSE`
- `profile_id`: internal storage ID only, not user-facing
- `type`: assistant, companion, character, health_coach, lab, etc.
- `status`: active, archived

Until this is cleaned up, avoid casual rename behavior that could desynchronize display names from runtime Vantage IDs.
