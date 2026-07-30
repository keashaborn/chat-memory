# AI Response Preferences V1 — Integration Handoff

Status: isolated integration candidate only
Backend original base: `80f21e0f477eec538d118d897d19afa51c2738ef`
Backend prior refreshed base: `1fdf3832787f060a36a15a117f0d5f0afded57db`
Backend integration base: `d11d3bdf15a225899f9abacade819c159fe116cc`
Frontend base: `11cfad46ec959fb27359836158583ef1faedf94f`
Frontend candidate head: `74c3df708cfd9670c640df4ffd6522cd83095f5b`

## Purpose

This candidate replaces the Qdrant `memory_raw` personalization card with one
explicit, owner-scoped PostgreSQL settings record. It is part of the stable
response runtime. It is not personal Memory V1, Fractal Monism retrieval, a
Vantage/persona, or model training.

The response runtime remains usable when Memory V1 and FM retrieval are absent:

```text
authenticated request
  -> safety and response-mode policy
  -> interaction decision
  -> explicit AI response preferences
  -> bounded prompt assembly
  -> provider request
  -> content-free inspection
```

Governed Memory V1, FM corpus retrieval, web evidence, and structured
LifeSwitch data remain independent typed inputs. None may use response
preferences as an owner, authorization source, retrieval filter, or evidence.

## User controls

- Optional assistant name.
- Nickname, occupation, and bounded “more about you” context.
- Bounded custom response instructions.
- Response length: concise, balanced, detailed.
- Technical depth: plain, balanced, expert.
- Format: auto, prose, bullets, steps.
- Conversation style: direct, natural, warm.

There is no separate friendliness dial. `Warm` is the single friendliness
choice and compiles to a friendly, expressive tone without fake empathy,
flattery, excessive reassurance, or automatic agreement.

## Authority and safety

- Supabase authentication establishes the owner UUID.
- Supabase user metadata is not a preference authority. The browser obtains the
  saved conversation style from the authenticated preference API and retains
  only a local delivery cache for voice playback.
- PostgreSQL RLS and `FORCE ROW LEVEL SECURITY` bind the row to
  `app.user_id`.
- The browser cannot supply preferences to `/response/query`.
- The backend loads preferences independently by authenticated owner.
- Preferences cannot modify safety, response mode, interaction selection,
  memory selection, FM routing, tools, ownership, or factual standards.
- High-stakes mode suppresses profile, style, format, depth, length, and custom
  instructions. The optional conversational assistant name may remain.
- Obvious control-language in custom instructions is stored but not projected.
- Free text is normalized, bounded, JSON-quoted, and labeled lower authority.
- Defaults add zero preference tokens to the prompt.
- Rendered profile/custom text is additionally capped.

## Persistence

Candidate migration:

`ops/sql/20260730_assistant_response_preferences_v1.sql`

Target:

`user_settings.assistant_response_preference_v1`

The row is owner keyed and revisioned. PUT uses optimistic concurrency through
`expected_revision`; stale updates return HTTP 409. No legacy Qdrant card is
imported automatically.

Revision zero uses insert-only semantics. Existing records use an explicit
owner-and-revision-guarded update. This avoids an invalid `INSERT ... SELECT`
upsert shape that the disposable database preflight caught before activation.

API:

```text
GET /assistant-preferences/{authenticated_owner_uuid}
PUT /assistant-preferences/{authenticated_owner_uuid}
```

Frontend proxy:

```text
GET /api/user/assistant-preferences
PUT /api/user/assistant-preferences
```

## Prompt and inspection

The typed record is bound through:

```text
AuthenticatedResponseCommandV0_2
  -> TrustedResponseRequestV0_2
  -> PromptAssemblyRequestV1
  -> PromptAssemblyManifestV1
```

Changed wire identifiers were advanced:

- `trusted_response_request_v0_4`
- `trusted_response_plan_v0_5`
- `trusted_response_orchestrator_v0_5`
- `prompt_assembly_request_v2`
- `assembled_prompt_v2`
- `prompt_assembly_manifest_v5`
- `typed_prompt_assembler_v4`

Inspector adds only:

- source: defaults or postgres;
- status: defaults, applied, partial, or suppressed;
- applied/suppressed/truncated counts;
- assistant-name/custom-instruction inclusion booleans;
- high-stakes override boolean;
- estimated tokens.

No name, background, custom instruction, owner UUID, row revision, or prompt
content enters the inspection payload.

## Retired in this candidate

Frontend:

- `app/api/user/instructions/route.ts`
- Qdrant `/cards` persistence from Personalization.

Backend:

- `rag_engine/assistant_name_preference_provider_v1.py`
- `rag_engine/assistant_name_preference_v1.py`
- Their obsolete Qdrant-focused test.

Historical `resse_user_preferences.py` and its offline evaluation artifacts are
not in the live request path. They remain unchanged for later evidence-preserving
retirement review.

## Validation

Backend:

- Python compilation passed.
- 297 focused response, prompt, interaction, inspection, preference, ownership,
  Memory boundary, FM boundary, provider, persistence, and legacy-regression
  tests passed on the rebased candidate.
- The candidate migration passed in a disposable PostgreSQL 16 container:
  owner read, cross-owner read/update/insert denial, legitimate revision
  update, stale-revision rejection, and automatic container teardown.
- Two committed Memory V1 schema-snapshot tests fail identically on unchanged
  production and the candidate. They are recorded as pre-existing Memory
  schema drift; this candidate does not regenerate or modify those schemas.
- `git diff --check` passed after the handoff whitespace cleanup.

Frontend:

- 15 personalization and Response Trace tests passed.
- Prettier check passed.
- The normal production-environment preflight passed without printing values.
- The complete Next.js production build passed: compilation, TypeScript,
  72-page static generation, build traces, and Turnstile artifact verification.
- `VS_DEV_ALLOW_GUEST` is disabled in the live service environment.
- Temporary environment links were removed after the isolated build.

No production checkout, service, database, Qdrant collection, environment
variable, authentication rule, Memory V1 record, FM corpus, or live prompt was
changed.

## Required review before activation

1. Reconfirm both production heads immediately before promotion.
2. Complete joint Memory V1 review of the three shared boundaries:
   `response_composition_root_v0_2.py`,
   `response_orchestration_v0_2.py`, and `prompt_assembler_v1.py`.
3. Response-policy review verifies high-stakes precedence and contract-version
   changes.
4. Review exact diffs and approve migration/promotion separately.
5. Apply the database migration before backend activation.
6. Promote backend, then frontend, run authenticated two-owner canaries, and
   verify content-free Inspector output.

Rollback does not require dropping the settings table. Reverting the application
commits makes the table inert; table removal is a separate destructive action.
