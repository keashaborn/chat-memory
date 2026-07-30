# AI Response Preferences V1 — Integration Handoff

Status: isolated candidate only  
Backend original base: `80f21e0f477eec538d118d897d19afa51c2738ef`  
Backend refreshed base: `1fdf3832787f060a36a15a117f0d5f0afded57db`  
Frontend base: `11cfad46ec959fb27359836158583ef1faedf94f`

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
- 159 focused response, prompt, interaction, inspection, preference, ownership,
  and legacy-regression tests passed.
- `git diff --check` passed.

Frontend:

- 15 personalization and Response Trace tests passed.
- Prettier check passed.
- Next.js compiled and TypeScript passed.
- Static prerender then stopped because the isolated worktree intentionally had
  no Supabase environment values (`supabaseUrl is required`).

No production checkout, service, database, Qdrant collection, environment
variable, authentication rule, Memory V1 record, FM corpus, or live prompt was
changed.

## Required review before activation

1. Refresh both production heads and check for overlapping work.
2. Memory V1 reviews the three shared boundaries:
   `response_composition_root_v0_2.py`,
   `response_orchestration_v0_2.py`, and `prompt_assembler_v1.py`.
3. Response-policy review verifies high-stakes precedence and contract-version
   changes.
4. Apply the candidate SQL in a disposable database and prove RLS isolation,
   optimistic-concurrency behavior, and rollback.
5. Run the frontend build with its normal deployment environment.
6. Review exact diffs and approve migration/promotion separately.
7. Apply the database migration before backend activation.
8. Promote backend, then frontend, run authenticated two-owner canaries, and
   verify content-free Inspector output.

Rollback does not require dropping the settings table. Reverting the application
commits makes the table inert; table removal is a separate destructive action.
