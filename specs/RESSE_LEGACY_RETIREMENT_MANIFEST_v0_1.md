# RESSE Legacy Identity and Tuning Retirement Manifest v0.1

Status: proposed; inventory and migration plan only
Date: 2026-07-19
Runtime changes authorized: no

## 1. Baselines inspected

Read-only inspection was performed against:

- Verbal Sage: `/var/www/verbalsage-chat_v2`
- Verbal Sage commit: `9400f777965c74ac346db567d179cbb0ee4f6e93`
- Verbal Sage branch: `main` (clean; ahead of `origin/main` by 719 commits)
- Isolated seebx worktree: `/home/ubuntu/chat-memory-resse-runtime-v0_1`
- Isolated RESSE commit before this manifest: `dabdc75cc7b123a1effe3de40121729ce2b01e56`

The production seebx checkout, production services, databases, Qdrant, and
Memory V1 were not modified or queried by this inventory.

## 2. Target state

- One backend-owned assistant identity: `RESSE`.
- No identity selector, custom identity builder, profile presets, tuning
  sliders, or user-controlled retrieval weights.
- One Personalization page containing governed memory, user profile context,
  and bounded response preferences.
- Response mode, FM eligibility, retrieval budgets, memory intent, and safety
  remain backend-owned.
- Legacy values may be accepted temporarily for compatibility, but they have
  no authority after the new policy becomes active.

## 3. Legacy identifiers

### 3.1 Client cookies

- `vs_vantage_id`
- `vs_vantage_mix`
- `vs_vantage_routing`
- `vs_vantage_limits`
- `vs_vantage_pragmatics`
- `vs_vantage_definition_overlay`
- `vs_vantage_roleplay`

### 3.2 Browser and account profile storage

- local storage `vs_vantage_profiles`
- local storage `vs_vantage_default_id`
- local storage `vs_vantage_presets`
- Supabase user-metadata key `vs_vantage_profiles_v1`

### 3.3 Client request fields

- `vantage_id`
- `limits`
- `routing`
- `mix`
- `pragmatics`
- `roleplay`
- `definition_overlay`
- alternate `assistant_profile_id` values

### 3.4 Forwarded headers

- `X-VS-Vantage-Id`
- `X-VS-Mix`

### 3.5 Permission capabilities

- `assistant_profile.view`
- `assistant_profile.apply_builtin`
- `assistant_profile.create_custom`
- `assistant_profile.edit_basic`
- `assistant_profile.edit_advanced`
- `assistant_profile.edit_admin_levers`

The replacement Personalization page requires ordinary account-level view and
edit permissions. It must not inherit the identity or tuning capability model.

## 4. Verbal Sage retirement map

### 4.1 Replace user-facing identity surfaces

| Surface | Current responsibility | Target |
|---|---|---|
| `components/nav/AccountMenu.tsx` | Links to Assistant Profile | Link to Personalization |
| `app/settings/assistant-profile/page.tsx` | Profile selection and profile-scoped editor | Redirect to `/personalization` after replacement is live |
| `components/admin/settings/VantageProfilePage.tsx` | Built-ins, custom profiles, sliders, presets | Retire from user runtime |
| `components/admin/settings/VantagePersonalizationEditor.tsx` | Vantage-scoped instruction editor with Morgan/Riley defaults | Replace with user-scoped custom-instruction editor |
| `components/admin/SettingsDrawer.tsx` | Applies and displays profile state | Remove profile-specific sections and apply action |
| `components/admin/settings/permissions/permissionRegistry.ts` | Grants six profile/tuning capabilities | Retire capabilities after route replacement |
| `components/admin/settings/AdminConsolePage.tsx` | Describes profile levers in admin inventory | Remove or replace with fixed-policy diagnostics |

### 4.2 Replace personalization data flow

| Surface | Current responsibility | Target |
|---|---|---|
| `app/personalization/page.tsx` | Reads/writes vantage-scoped `user_instructions`; exposes active vantage and raw card | New Memory, Custom instructions, nickname, occupation, and More about you page |
| `app/api/user/instructions/route.ts` | Reads/writes `user_instructions` by `vantage_id` or cookie | Authenticated user-scoped preference/profile contract; final storage requires Memory V1 review |
| `components/admin/settings/store.tsx` | Reads and writes all Vantage cookies and applied state | Ordinary settings only; stop emitting legacy profile state |

### 4.3 Shared chat integration hold

These files are live shared integration points and must not be changed until
the backend-owned policy and final Memory V1 interfaces are jointly reviewed:

- `app/api/chat/route.ts`
- `app/api/chat/inspect/route.ts`

They currently read legacy body fields and cookies, enforce profile
permissions, send tuning fields to `/vantage/query`, and forward legacy
headers. Their removal must be coordinated with the seebx consumer change in
the same integration window.

### 4.4 Diagnostic and administrative review

These references require classification during implementation. They are not
automatically deleted because some may remain useful as historical or
fixed-policy diagnostics:

- `app/developer/diagnostics/page.tsx`
- `app/api/admin/vantage-cards/route.ts`
- `components/admin/settings/CardsPanel.tsx`
- `components/assistant-ui/threadlist-sidebar.tsx`
- `components/auth/AuthGate.tsx`
- `docs/VANTAGE_SETTINGS_PLAN.md`
- `docs/verbal_sage_product_architecture_roadmap.md`

## 5. seebx replacement map

### 5.1 Shared runtime integration hold

| Surface | Current responsibility | Replacement boundary |
|---|---|---|
| `rag_engine/vantage_router.py` | Accepts and applies `limits`, `routing`, `mix`, `pragmatics`, roleplay, definition overlay, and `vantage_id` | Backend-owned response mode, retrieval contract, and preference envelope |
| `rag_engine/prompt_builder.py` | Loads persona, user instructions, Vantage preferences, and profile-card blocks | Explicit authority-ordered blocks after Memory V1 review |
| `rag_engine/persona_loader.py` | Vantage-scoped and legacy fan-in for persona, instruction, preference, and profile cards | Governed user profile/preferences without RESSE ownership alias |
| `rag_engine/retriever_unified.py` | Per-vantage RAG policy and Vantage payload filtering | Mode-owned FM eligibility and independent Memory V1 retrieval |
| `rag_engine/telemetry_router.py` | Persists Vantage identifiers with response telemetry | Preserve historical compatibility or migrate to policy version/mode fields |
| `app.py` | Vantage namespacing in threads and card APIs | Requires owner and Memory V1 API review; do not bulk-remove |

### 5.2 Existing isolation artifacts

The proposed replacements already exist only on the isolated branch:

- `rag_engine/resse_runtime_policy.py`
- `rag_engine/resse_user_preferences.py`
- `specs/RESSE_RUNTIME_POLICY_v0_1.md`
- `specs/RESSE_RETRIEVAL_CONTRACT_v0_1.yaml`
- `specs/RESSE_PERSONALIZATION_CONTRACT_v0_1.md`
- `specs/RESSE_USER_PREFERENCE_CONTRACT_v0_1.md`
- `evals/resse_behavior_cases_v0_1.jsonl`
- `evals/resse_preference_cases_v0_1.jsonl`

They have no production imports or side effects.

### 5.3 Compatibility and data holds

Do not delete or rewrite these merely because their names contain `vantage`:

- Memory V1 owner-resolution, evidence, artifact, extraction, promotion, and
  review code;
- historical telemetry rows containing `vantage_id`;
- legacy `user_instructions` cards before export and owner-verified migration;
- `public.vs_profiles` and `vantage_identity.rag_policy` before archival review;
- historical Qdrant payloads or points;
- archive directories and migration documentation.

The current `vantage_id` field sometimes represents identity, sometimes a
card namespace, sometimes compatibility fan-in, and sometimes historical
telemetry. A global search-and-delete would corrupt ownership and provenance.

## 6. Replacement order

### Phase A — completed isolated prerequisites

- Define and test backend-owned response modes.
- Define FM retrieval authority and budgets.
- Define bounded user preferences and relevance selection.
- Establish behavior and preference evaluation fixtures.

### Phase B — Memory V1 integration review

- Confirm final governed memory-intent and profile-context interfaces.
- Define user-scoped nickname, occupation, More about you, and custom
  instruction storage.
- Define migration semantics for existing Vantage-scoped instruction cards.
- Confirm `RESSE` never becomes an owner, namespace, or filter.

### Phase C — backend shadow integration

- Add the RESSE policy adapter behind a server-side feature flag.
- Compute new and legacy decisions in shadow without changing responses.
- Compare mode, retrieval, preference, latency, and suppression traces.
- Do not log private profile text or assembled prompts.

### Phase D — backend authority cutover

- Make the RESSE policy authoritative.
- Ignore legacy tuning fields and cookies while recording bounded counters.
- Preserve rollback to the last known-good backend policy.
- Verify Memory V1, authentication, ownership, and structured application data.

### Phase E — frontend cutover

- Deploy the simplified Personalization page.
- Stop emitting legacy fields and headers.
- Clear legacy cookies and local caches after the server no longer depends on
  them.
- Redirect Assistant Profile to Personalization.
- Remove profile-management capabilities and UI.

### Phase F — compatibility retirement

- Remove dead frontend and backend readers after an observation window.
- Export and owner-verify legacy instructions and profile presets.
- Archive or remove preserved database/Qdrant state only under a separately
  approved deletion and restoration plan.

## 7. Rollback boundaries

- Frontend cutover must be reversible independently of backend authority.
- Backend policy cutover must have one feature flag with a documented
  last-known-good value.
- Memory and profile migrations require idempotent scripts and explicit
  rollback behavior.
- Do not make destructive schema or Qdrant changes part of the response-policy
  deployment.
- Preserve legacy values during the observation window but do not allow them
  to override the new policy.

## 8. Verification gates

Before production cutover:

- all isolated RESSE behavior and preference cases pass;
- shared integration tests pass for chat, inspect, authentication, owner
  resolution, Memory V1 intent, retrieval, and structured application data;
- a Verbal Sage production build succeeds in the separate worktree;
- requests contain no user-authoritative identity or tuning controls;
- high-stakes and technical FM suppression remains deterministic;
- `RESSE` never appears as a memory owner or filter;
- frontend and backend policy versions are observable without logging private
  content;
- rollback is tested before removal of any compatibility reader.

## 9. Explicit non-actions

This manifest does not authorize:

- edits to live seebx or Verbal Sage checkouts;
- prompt-builder, router, persona-loader, chat-route, authentication, or Memory
  V1 modifications;
- deployment or service restart;
- database migration;
- Qdrant mutation;
- removal of legacy cards, rows, tables, collections, or telemetry.
