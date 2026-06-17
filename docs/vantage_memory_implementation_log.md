# Vantage Memory Implementation Log

This document is append-only. The original audit/source document remains the baseline for what we intended to fix. This file records what was actually changed, verified, and deployed.

---

## 2026-06-15 — Vantage Registry Mirror and Frontend Sync

### Completed

Created a Brains-side Vantage registry mirror so backend jobs can discover Vantages by `user_id` and `vantage_id`.

Supabase remains the source of truth for account settings and saved Vantage presets. Brains now mirrors those Vantages into Postgres.

Added Postgres schema/table:

```text
vantage_profile.registry
```

Added Brains endpoints:

```text
POST /vantages/sync
GET  /vantages/{user_id}
```

Added two sync modes:

```text
mode: full
  Mirrors saved Supabase Vantage presets.
  Can remove mirrored Supabase rows no longer present in the saved preset payload.

mode: active
  Updates the currently active/applied Vantage.
  Does not delete saved Vantage rows.
```

Verified live registry row:

```text
user_id: 1240822d-ac9a-4096-95aa-e2b24d36ef50
vantage_id: RESSE
profile_id: 76da1e00
is_default: true
is_active: true
```

### Frontend Changes

Added Next.js route:

```text
app/api/vantages/sync/route.ts
```

This route reads the `vs_at` auth cookie, verifies the Supabase JWT, uses the JWT subject as `user_id`, and forwards Vantage sync payloads to Brains.

Updated:

```text
components/admin/settings/VantageProfilePage.tsx
components/admin/settings/store.tsx
```

Saved preset changes now call full Vantage sync.

Header Save / active Vantage save now calls active-only sync.

### Verification

Backend:

```text
/readyz returned ok.
Python compile passed.
Full sync returned 200.
Active-only sync returned 200.
GET /vantages/{user_id} returned RESSE row.
```

Frontend:

```text
npx tsc --noEmit passed.
npm run build passed.
verbalsage-v2.service restarted successfully.
/api/vantages/sync appeared in the Next build.
Browser active sync returned ACTIVE_SYNC_STATUS 200.
```

### Meaning

The basic bridge now works:

```text
Browser
  -> Next /api/vantages/sync
  -> Brains /vantages/sync
  -> Postgres vantage_profile.registry
```

Brains can now know which Vantages exist for a user and which one is active.

---

## 2026-06-16 — Registry-Driven Daemon and Vantage-Scoped Learning Pipeline

### Completed

Converted the Vantage initiator from a hard-coded, one-service-per-Vantage setup into a registry-driven daemon.

Before:

```text
vantage-initiator.service        -> default
vantage-initiator-resse.service  -> RESSE
```

After:

```text
vantage-initiator.service        -> --all-from-registry
vantage-initiator-resse.service  -> stopped/disabled
```

The daemon now reads Vantages from:

```text
vantage_profile.registry
```

and processes each mirrored Vantage automatically.

Verified that both current Vantages are discovered and processed:

```text
RESSE
EVA
```

Created `EVA` through the UI and confirmed the full path works:

```text
UI saved Vantage
  -> Supabase metadata
  -> frontend /api/vantages/sync
  -> Brains /vantages/sync
  -> Postgres vantage_profile.registry
  -> registry-driven daemon
```

### Backend Vantage Registry Status

Verified registry rows:

```text
RESSE
  is_default: true
  is_active: true

EVA
  is_default: false
  is_active: false
```

Verified controller config exists for:

```text
default
RESSE
EVA
```

The daemon automatically created controller config for EVA by copying default controller settings.

### Fact and Card Pipeline Changes

Removed the `Key: Value`-only bottleneck from fact seeding.

Previously:

```text
fact_seed_from_chat_log_v1 only seeded user messages containing Key: Value lines.
```

Now:

```text
fact_seed_from_chat_log_v1 seeds recent user chat rows for the current Vantage.
```

Added conservative deterministic natural-language feedback extraction in:

```text
fact_jobs.py
```

New extractor:

```text
parse_nl_feedback_facts
```

It currently detects obvious user feedback patterns such as:

```text
concise / short responses
avoid long responses
no bullet points
prose over bullets
avoid numbered answers
direct / straightforward style
less generic / more specific
positive helpfulness feedback
```

### Vantage Scoping Fixes

Made fact extraction Vantage-scoped.

```text
fact_extract_once(conn, vantage_id=...)
```

Made card consolidation Vantage-scoped.

```text
card_consolidate_from_kv_once(conn, vantage_id=...)
```

Made fact drives Vantage-scoped.

```text
compute_fact_drives(conn, vantage_id=...)
```

Verified scoped drives:

```text
RESSE
  pending_sources > 0
  active_claims > 0

EVA
  pending_sources: 0
  active_claims: 0
```

This confirms that RESSE learning is not bleeding into EVA.

### Verified Natural-Language Learning

Confirmed ordinary RESSE chat messages now become fact sources.

Verified natural-language feedback evidence:

```text
attr.response_length = concise
attr.format = no_bullet_points
attr.specificity = less_generic
```

Verified RESSE preference cards were created:

```text
pref/format: no_bullet_points
pref/response_length: concise
pref/specificity: less_generic
```

This is the first confirmed end-to-end path from ordinary user speech into Vantage-scoped preference cards.

### Cleanup Completed

Patched no-op fact extraction scheduling.

Before:

```text
fact_extract_v1 was sometimes scheduled for EVA even when EVA had no pending sources.
```

After:

```text
fact_extract_v1 is only scheduled when the current Vantage has pending_sources > 0.
```

Verified newer EVA ticks show:

```text
pending_sources: 0
```

and no unnecessary `fact_extract_v1` enqueue for EVA.

### Current Safe State

```text
Brains healthy.
Frontend healthy.
Registry-driven daemon running.
RESSE and EVA discovered from registry.
Natural-language feedback enters fact pipeline.
RESSE preference cards created from ordinary speech.
Vantage scoping is enforced for extraction, drives, and consolidation.
```

### Remaining Issues / Follow-Up

The new preference cards exist, but the next major question is whether the live response path actually uses them.

Next target:

```text
Prompt/persona path should retrieve Vantage-scoped preference cards and inject them into the live response prompt.
```

Known future issue:

```text
Personalization/instructions appear user-global rather than Vantage-specific.
```

This should eventually be split into:

```text
base user personalization
Vantage-specific instructions
Vantage-specific role/persona overlay
```

---

## 2026-06-16 — Live Vantage Profile Learning Verified

Verified the full live Vantage memory loop:

ordinary user speech -> public.chat_log -> vantage_fact.source -> extracted claims/evidence -> vantage_card.card_head -> Memory Cards UI -> live system prompt injection -> response adaptation

New Vantage card categories verified:
- identity/preferred_name
- background/profession
- background/company_history
- background/status
- project/current_project
- project/current_focus
- pref/voice

Live app test message was processed:
I should mention that I am retired now and most of my current work is focused on Verbal Sage.

It produced live RESSE cards:
- background/status: retired
- project/current_focus: Verbal Sage

Live prompt now includes both:
- [VANTAGE PREFERENCE CARDS]
- [VANTAGE PROFILE CARDS]

Legacy Qdrant memory cards, gravity_profile, and vb_desire_profile still exist and should be treated as legacy/read-only until audited.

---

## 2026-06-16 — Legacy Gravity Containment

Disabled the legacy nightly gravity rebuild timer:

- gravity-rebuild.timer disabled
- gravity-rebuild.service left installed but inactive

Reason: gravity_profile is user-global / legacy Qdrant memory, not Vantage-native. It should not silently shape RESSE, EVA, or future roleplay Vantages.

Current containment rule:

- Named Vantages such as RESSE and EVA should not use legacy/global gravity unless explicitly enabled.
- Backend fallback namespace "default" is distinct from the user-facing default preset RESSE.
- Qdrant memory_raw is kept for semantic retrieval, but legacy global influence should be audited before reuse.
