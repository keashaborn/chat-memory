# Vantage Memory Architecture Audit
Date: 2026-06-15

## Executive Summary

The current Verbal Sage memory system contains three partially overlapping architectures:

1. Legacy user-global memory architecture.
2. Newer vantage-aware card architecture.
3. Experimental relational fact/card architecture.

Most inconsistencies observed during this audit result from these systems operating simultaneously.

The long-term direction should be a unified `(user_id, vantage_id)` architecture in which the vantage is the primary unit of personalization, memory, behavioral adaptation, card generation, and prompt injection.

Latest audit finding: Supabase is already the canonical cross-browser store for saved Vantage presets and the last active applied Vantage. Brains/Postgres receives active `vantage_id` during chat, but it does not currently own or mirror the saved Vantage registry. That prevents Brains background jobs from independently discovering and processing every saved Vantage.

---

# Verified Findings

The items below were verified directly from code inspection and live system behavior.

## Frontend

The frontend currently treats the active vantage as the primary interaction context.

Active vantage is stored in:

```text
vs_vantage_id
```

and is used by:

```text
chat routes
feedback routes
personalization routes
```

Personalization is already scoped to the active vantage.

Current personalization sections:

```text
About Me
Response Style
Workflow
Output Format
Boundaries
```

These are stored as a `user_instructions` card associated with the active vantage.

---

## Identity Bridge

Supabase auth is the real user identity source.

Verified live user id:

```text
1240822d-ac9a-4096-95aa-e2b24d36ef50
```

This is the same user id Brains has been using in chat logs, cards, and memory.

Frontend `/api/identity` uses the Supabase JWT subject as `user_id`.

It checks Brains for an existing `user_identity` card. If missing, it logs:

```text
source = frontend/identity
text = FULL_NAME:<name>
```

Brains special-cases this identity log and writes a Qdrant `user_identity` card.

Brains has an old alias table:

```text
vantage_identity.user_alias
```

Current rows only map old/default aliases such as `anon`, `guest`, and `debug` to the same canonical user id. This appears to be legacy/default cleanup, not the main identity path.

Future tables should use:

```text
user_id = Supabase auth sub
```

and should not rely on `user_alias` for the primary architecture.

---

## Vantage Persistence

Current Vantage persistence is split across Supabase, browser storage, runtime cookies, and Brains.

### Saved Named Vantage Presets

Saved named Vantage presets are stored in Supabase user metadata:

```text
user_metadata.vs_vantage_profiles_v1
```

Verified live shape:

```text
defaultId
profiles
updated_at
```

Each profile contains:

```text
id
name
state
created_at
updated_at
```

The profile `state` contains:

```text
vantageId
limits
routing
mix
roleplay
pragmatics
```

There is also a local browser cache:

```text
localStorage.vs_vantage_profiles
localStorage.vs_vantage_default_id
```

The code comments state that Supabase is authoritative for cross-browser consistency.

Verified live saved profile:

```text
id: 76da1e00
name: RESSE
state.vantageId: RESSE
created_at: 2026-01-05T21:50:15.237Z
updated_at: 2026-01-11T00:22:11.743Z
```

Verified live registry timestamp:

```text
vs_vantage_profiles_v1.updated_at: 2026-01-17T01:21:51.453Z
```

### Last Active Applied Vantage

The last active applied Vantage is stored in Supabase user metadata:

```text
user_metadata.vs_settings_v1
```

Verified live active state:

```text
model: gpt-5.2
vantage.active.vantageId: RESSE
updated_at: 2026-05-18T04:53:20.372Z
```

The live active Vantage contains:

```text
limits
mix
routing
pragmatics
roleplay
```

This allows a new browser session to hydrate the active Vantage configuration from Supabase.

### Runtime Cookies

The chat runtime does not read Supabase directly. It reads cookies.

Current runtime cookies include:

```text
vs_model
vs_vantage_id
vs_vantage_mix
vs_vantage_routing
vs_vantage_limits
vs_vantage_pragmatics
vs_vantage_roleplay
```

The frontend `/api/chat` route reads these cookies and forwards the active Vantage settings to Brains `/vantage/query`.

### AuthGate Hydration

`AuthGate.tsx` hydrates cookies from Supabase `vs_settings_v1` during session bootstrap.

Verified call order:

```text
applyProfileCookiesFromSession(s)
void applyDefaultProfileBestEffort()
void syncIdentityBestEffort(s)
```

So Supabase gets first chance to hydrate the active Vantage.

The old Brains profile fallback only runs if no `vs_vantage_id` cookie exists.

### Server Auth Cookie

The Supabase browser session and the Next.js server auth cookie are separate layers.

```text
Supabase browser session
  -> lets the React app know the user is logged in

vs_at HttpOnly cookie
  -> lets Next.js API routes prove the user server-side
```

A stale browser session was observed where `/api/auth/whoami` returned:

```text
401 no vs_at cookie
```

After logout/login, `/api/auth/whoami` returned status 200 and exposed the expected metadata. This means the auth bridge works after a fresh login, but stale sessions may temporarily lack the server cookie.

### Old Brains Profile Fallback

`/api/profiles/apply_default` still exists and is called by `AuthGate`.

It fetches:

```text
BRAINS_URL/profiles/{user_id}/default
```

and writes:

```text
vs_model
vs_vantage_id
vs_vantage_mix
```

This appears to be a legacy fallback path, not the current primary Vantage persistence mechanism.

### Current Persistence Mismatches

Supabase saves more active Vantage fields than `AuthGate` currently hydrates.

Supabase `vs_settings_v1` includes:

```text
pragmatics
roleplay
```

But `AuthGate.tsx` currently hydrates only:

```text
model
vantage_id
mix
routing
limits
```

It does not hydrate:

```text
vs_vantage_pragmatics
vs_vantage_roleplay
vs_vantage_definition_overlay
```

There is also a cookie-name mismatch:

```text
settings/store.tsx writes roleplay to:
  vs_vantage_definition_overlay

app/api/chat/route.ts reads roleplay from:
  vs_vantage_roleplay
```

Brains accepts the field as:

```text
definition_overlay
```

So the backend can receive the overlay, but the frontend runtime route may not send it from cookie state because it reads a different cookie name than the settings store writes.

### Saved Preset vs Active Snapshot

The saved RESSE preset and the active RESSE snapshot are separate concepts and can differ.

Verified example:

```text
Saved RESSE preset:
  pragmatics.rfg = 0.69
  pragmatics.df = 0.71

Active RESSE snapshot:
  pragmatics.rfg = 0
  pragmatics.df = 0.7
```

This distinction is valid, but Brains should eventually know both:

```text
saved named Vantage registry
last active/applied Vantage state
```

### Architectural Meaning

Supabase is currently the cross-browser persistence layer for saved Vantage presets and the last active applied Vantage.

Cookies are the runtime transport layer used by frontend API routes.

Brains/Postgres receives active `vantage_id` during chat and stores it in logs/cards, but Brains does not currently own or enumerate the full saved Vantage registry.

This matters because background memory/card processing in Brains cannot process all saved user Vantages unless the saved Vantage registry is either synced into Brains or queried from Supabase.

---

## Memory Cards

Current card types include:

```text
user_identity
assistant_identity
user_instructions
style
style_mode
preference
gravity_profile
vb_desire_profile
persona_profile
```

Cards are stored in:

```text
Qdrant memory_raw
```

The newer card API already supports:

```text
user_id
vantage_id
kind
topic_key
```

and is capable of maintaining separate cards per vantage.

---

## Gravity Profile

Current implementation:

```text
compute_gravity(user_id)
```

Behavior:

```text
loads memories by user_id only
ignores vantage_id
creates a single gravity profile for the user
```

Result:

Multiple vantages are collapsed into a single gravity profile.

---

## Desire Profile

Current implementation:

```text
build_vb_desire_profile(user_id)
```

Behavior:

```text
loads memories by user_id only
ignores vantage_id
creates a single desire profile for the user
```

Result:

Multiple vantages are collapsed into a single desire profile.

---

## Fact Pipeline

Current fact extraction is based on structured:

```text
Key: Value
```

entries.

Example:

```text
Preference: concise
Format: prose
```

Natural conversational feedback is not captured.

Example:

```text
I like shorter responses.
Use more prose.
Stop using bullet points.
```

These statements currently do not enter the fact pipeline.

---

## Cards UI

Current Memory Cards UI filters by:

```text
card type
```

Examples:

```text
Gravity
Desire
Identity
Style
```

The UI does not provide a vantage selector.

---

## Initiator Architecture

Current initiator processing is tied to a specific vantage.

Verified configuration:

```text
vantage-initiator.service
  -> default

vantage-initiator-resse.service
  -> RESSE
```

The current architecture processes one vantage per service instance.

It does not automatically discover and process all active vantages.

---

## Persona Loader

The persona loader is already partially migrated to a vantage-aware model.

It loads cards using:

```text
user_id
vantage_id
```

and prefers cards scoped to the active vantage.

This portion of the architecture is already aligned with the intended direction.

---

# Architectural Mismatches

## Mismatch 1

Frontend is vantage-centric.

Behavioral memory remains partially user-centric.

---

## Mismatch 2

Behavioral profiles are global.

Product requirements are per-vantage.

---

## Mismatch 3

Fact extraction expects structured notes.

Users naturally provide conversational feedback.

---

## Mismatch 4

Cards are displayed through card-type filtering.

Users think in terms of vantages.

---

## Mismatch 5

No Brains/Postgres-side canonical Vantage registry has been identified.

A durable Brains-visible structure is needed to represent:

```text
user_id
vantage_id
display_name
settings
sliders
model configuration
last_used
created_at
updated_at
```

---

## Mismatch 6

Two card architectures currently coexist:

```text
Qdrant memory_raw
Postgres vantage_card
```

They are not fully reconciled.

---

## Mismatch 7

Supabase currently owns the saved Vantage preset registry, while Brains owns background memory/card processing.

Brains cannot currently enumerate all saved user Vantages unless the Vantage registry is synced into Brains or queried from Supabase.

---

# Target Architecture

## Core Principle

The vantage becomes the primary unit of memory and personalization.

Everything behavioral is attached to a specific vantage.

---

## Desired Model

```text
User
    ↓
Vantage
    ↓
Personalization
    ↓
Memory Events
    ↓
Behavioral Signals
    ↓
Cards
    ↓
Prompt Injection
```

---

## User Layer

Stores stable account-level information.

Examples:

```text
account identity
authentication
subscription state
global user facts
```

---

## Vantage Layer

Stores:

```text
name
sliders
model settings
behavior configuration
```

A user may own multiple vantages.

---

## Personalization Layer

Stores:

```text
about me
response style
workflow
output format
boundaries
```

Associated with a specific vantage.

---

## Memory Layer

Stores raw interaction history.

Keyed by:

```text
user_id
vantage_id
```

---

## Signal Layer

Transforms natural conversation into structured behavioral signals.

Examples:

```text
prefers concise prose
likes philosophical framing
dislikes numbered lists
prefers challenge over agreement
```

No special trigger phrases should be required.

---

## Card Layer

Cards become summaries of accumulated signals.

Examples:

```text
gravity_profile
vb_desire_profile
style_profile
preference_profile
persona_profile
```

All behavioral cards should be scoped by:

```text
user_id
vantage_id
kind
topic_key
```

---

## User Interface

Memory Cards should be organized by vantage.

Preferred flow:

```text
Select Vantage
    ↓
Show All Cards For That Vantage
```

Current flow:

```text
Select Card Type
    ↓
Hide Everything Else
```

---

# Recommended Direction

Supabase should remain the auth/account source.

Supabase can remain the frontend cloud settings source for saved named Vantage presets and the last active Vantage.

Brains/Postgres should mirror the saved Vantage registry for backend processing.

Cookies should remain a runtime cache only.

This gives the product cross-browser consistency while allowing Brains background jobs to enumerate and process all saved `(user_id, vantage_id)` pairs.

Recommended future backend table:

```text
vantage_registry
  user_id
  profile_id
  vantage_id
  name
  is_default
  is_active
  state jsonb
  source_updated_at
  created_at
  updated_at
```

Recommended sync trigger:

```text
POST /vantages/sync
```

Frontend should call this after:

```text
cloudSetPresets(...)
pushCloudActiveProfile(...)
```

Then the initiator daemon can discover active Vantages from Brains/Postgres instead of relying on one Linux service per Vantage.

---

# Open Questions

1. Should Supabase remain the authoritative saved Vantage registry, or should Brains/Postgres mirror it?
2. How should Brains discover all saved Vantages for background processing?
3. Which systems still operate globally by user rather than by vantage?
4. What should the conversational signal extraction pipeline look like?
5. Should PostgreSQL or Qdrant become the authoritative card store?
6. Should roleplay/definition overlay be restored as active product scope or removed as dead partial architecture?

---

# Next Audit Tasks

1. Decide whether Brains should mirror the Supabase Vantage registry.
2. Fix or document Supabase-to-cookie hydration parity.
3. Standardize the roleplay/definition-overlay field name.
4. Inventory remaining user-global memory systems.
5. Design natural-language signal extraction.
6. Decide the authoritative storage model for cards.
7. Replace Memory Cards card-type dropdown with a Vantage selector.
