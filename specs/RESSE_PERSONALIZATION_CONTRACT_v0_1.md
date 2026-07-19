# RESSE Personalization Contract v0.1

Status: proposed; specification only
Date: 2026-07-19
Runtime changes authorized by this document: no

## 1. Decision

Verbal Sage has one assistant identity: `RESSE`.

Assistant identity, safety policy, response-mode routing, retrieval authority,
memory ownership, and Fractal Monism eligibility are backend-owned. A user may
not create, rename, select, or tune an assistant identity.

The user-facing settings model has two independent concerns:

1. **Memory / About you** — governed user facts and preferences owned by Memory
   V1.
2. **Custom instructions** — bounded presentation and working-style
   preferences owned by the user-instructions feature.

These may appear on one Personalization page, but they must remain separate in
storage, authorization, retrieval, and prompt assembly.

## 2. Product model

### 2.1 Fixed identity

- `assistant_profile_id` is always `RESSE`.
- `RESSE` is not a user, memory owner, memory namespace, or retrieval filter.
- The UI does not show an identity selector or editable assistant name.
- Built-in identities including `MORGAN`, `RILEY`, `EVA`, `(none)`, and custom
  profiles are retired from the user experience.
- The backend must ignore or reject identity-selection attempts from clients.

### 2.2 Memory / About you

The first card on Personalization should be **Memory** once the Memory V1 API
and review interface are ready.

It may show:

- whether governed memory is enabled;
- a short explanation of what RESSE can remember;
- a count or summary of reviewed memory items;
- a link to view, correct, remove, or manage memories.

The card must not expose Qdrant, claim promotion, ownership identifiers,
vantage IDs, retrieval weights, or raw assembled prompts.

Background facts such as occupation, health history, goals, relationships,
projects, and recurring activities belong in governed Memory V1. They must not
be stored as an ungoverned free-form instruction block merely because the UI
labels the section “About you.”

Until Memory V1 supplies the required read/write contract, the Memory card is
feature-gated or omitted. No placeholder facts are written to legacy storage.

### 2.3 Custom instructions

Custom instructions are optional and user-scoped, not assistant-profile- or
vantage-scoped.

The initial UI should expose one field:

**How would you like RESSE to respond?**

Examples include concise versus detailed answers, preferred formatting,
technical depth, whether to avoid generic encouragement, and preferred
interaction style.

Custom instructions may affect presentation and workflow preferences. They
must not:

- change assistant identity;
- override safety or high-stakes handling;
- select response mode;
- change FM corpus tiers, weights, or authority;
- change memory ownership, retrieval, extraction, or promotion;
- change authentication or account ownership;
- introduce tools or permissions;
- convert user background assertions into verified memory facts;
- act as universal system policy.

Free-form text is untrusted input. The backend applies length limits,
normalization, authorization, and injection boundaries before it is included
as a bounded user-preference block.

## 3. Controls to retire

Remove these user-facing capabilities:

- active assistant profile editing;
- assistant-profile selection;
- built-in profile switching;
- custom profile creation, overwrite, deletion, and default selection;
- profile-scoped personalization;
- thread-context weighting;
- personal-memory weighting;
- Fractal Monism corpus weighting;
- Fractal Monism lens weighting;
- similarity-threshold and recency-bias controls;
- answer-first, clarification tendency, and question-limit controls;
- conversational-opening controls;
- AI-disclaimer restraint;
- persona intensity;
- agreeability, evidence-revision, adaptation, and extra-wording sliders;
- roleplay, pragmatics, limits, mix, routing, and definition-overlay editors;
- raw Brains-card preview and active-vantage display.

Retiring a control means both removing it from the UI and preventing clients
from using the corresponding request field to change runtime behavior.

## 4. Legacy request fields

The runtime policy must ignore or reject the following client-controlled
fields after compatibility review:

- `vantage_id`
- `assistant_profile_id` values other than `RESSE`
- `limits`
- `mix`
- `routing`
- `pragmatics`
- `roleplay`
- `definition_overlay`
- profile presets and profile-default identifiers

Legacy cookies or local-storage values associated with these controls must not
become authoritative merely because an older browser continues sending them.
Removal of compatibility readers is a later migration step, after telemetry
confirms that the server-owned policy is active.

## 5. Target page

`Settings > Personalization`

Recommended order:

1. **Memory** — governed facts, status, and management link.
2. **Custom instructions** — “How would you like RESSE to respond?”
3. **Account preferences** — only ordinary application settings that do not
   affect assistant policy, if needed.

There is no separate Assistant Profile page. Existing links to
`/settings/assistant-profile` should eventually redirect to `/personalization`.

## 6. Current Verbal Sage surfaces affected later

Read-only inspection on 2026-07-19 identified these integration surfaces:

- `components/nav/AccountMenu.tsx`
- `app/settings/assistant-profile/page.tsx`
- `app/personalization/page.tsx`
- `components/admin/settings/VantageProfilePage.tsx`
- `components/admin/settings/VantagePersonalizationEditor.tsx`
- `components/admin/SettingsDrawer.tsx`
- `components/admin/settings/permissions/permissionRegistry.ts`
- `components/admin/settings/AdminConsolePage.tsx`
- `app/api/user/instructions/*`
- `app/api/chat/route.ts`
- `app/api/chat/inspect/route.ts`

The chat routes and user-instructions API require joint backend, frontend, and
Memory V1 integration review. They are explicitly out of scope for this
specification-only change.

## 7. Rollout sequence

1. Finish and audit Memory V1 ownership and retrieval behavior.
2. Integrate the backend-owned RESSE runtime policy.
3. Define the governed Memory card API and user correction/deletion flow.
4. Replace the Assistant Profile UI with the simplified Personalization page.
5. Stop emitting legacy tuning fields and cookies from the frontend.
6. Reject or ignore legacy fields server-side and record bounded telemetry.
7. Remove compatibility storage and dead profile code after verification.

No phase may silently migrate free-form “About you” prose into verified memory
claims. Migration requires extraction, provenance, user ownership, and normal
Memory V1 review rules.

## 8. Acceptance criteria

- Every normal chat uses backend-owned `RESSE` identity.
- No normal user can create or select another identity.
- No tuning slider changes prompt, retrieval, or response behavior.
- Custom instructions affect presentation only.
- Background facts are retrieved only through governed Memory V1.
- `RESSE` is never a memory owner or filter.
- High-stakes and technical modes suppress FM material as specified by the
  runtime policy, regardless of user instructions.
- Relevant authorized memory or structured application data remains available
  when permitted by the independent memory-intent router.
- Existing memory, authentication, owner-resolution, and service-token tests
  remain unchanged and passing.
- The simplified page exposes no raw prompt, Qdrant, claim, or vantage internals.

## 9. External design reference

OpenAI’s documented personalization model separates personality, custom
instructions, and memories. RESSE follows that separation but intentionally
removes the personality selector because identity is fixed. This reference is
a product-organization model, not a dependency or authorization to copy
OpenAI-specific storage behavior.
