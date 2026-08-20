# SeeBx assistant preferences capability v1

Status: isolated cleanup candidate; no deployment or production database change

Evidence date: 2026-08-20 UTC

## Decision

Retain assistant response preferences as a small platform account-settings
capability. They are not conversational memory, RAG, a profile ontology, a
prompt override, or tool authority.

The production frontend already exposes the feature and calls four authenticated
backend routes. The production backend router was removed with the original
Memory v1 chain even though its two forced-RLS PostgreSQL tables remained. The
live tables contain exactly one preference row and nineteen compilation
candidate rows. Deleting them or silently disabling the frontend would discard
active product state.

The cleanup candidate restores the feature under canonical owners:

- `seebx.capabilities.preferences.assistant_contracts`: strict wire and storage
  contracts;
- `seebx.capabilities.preferences.assistant_compiler`: untrusted prose to bounded
  typed review candidate;
- `seebx.capabilities.preferences.assistant_routes`: authenticated HTTP boundary;
- `seebx.adapters.assistant_preferences_postgres`: direct PostgreSQL and RLS
  adapter;
- `ops/sql/20260820_assistant_preferences_capability_v1.sql`: clean-install
  schema baseline.

No compatibility wrapper or `rag_engine` module remains.

## Canonical request flow

1. The Verbal Sage BFF forwards the original Supabase bearer token and asserted
   actor UUID.
2. SeeBx independently verifies the token, session, actor assertion, and exact
   path-owner match.
3. The request model rejects unknown fields, normalizes bounded text, and never
   returns input-bearing validation details.
4. The PostgreSQL adapter opens an owner connection and sets `app.user_id`.
5. Forced RLS and owner policies protect both preference and candidate rows.
6. Writes use optimistic `expected_revision` checks.
7. Free-form preference prose is compiled outside the database into a typed,
   hash-bound, 24-hour review candidate.
8. A second revision check stores the candidate. Nothing becomes active until
   the owner calls the separate approval route.
9. Approval locks the owner row and candidate, rejects stale/expired/consumed
   candidates, and atomically advances the preference revision.
10. The canonical response route reads the current preference record through
    the same owner-scoped PostgreSQL/RLS adapter.
11. The adapter projects only the four typed presentation enums and approved
    fixed rule IDs into `effective_assistant_preference_plan_v1`; it excludes
    narrative and profile text.
12. The typed prompt assembler binds the effective plan SHA-256 into its source
    request, manifest, system prompt, trusted response plan, and provider
    request evidence.

The exact HTTP surface is:

- `GET /assistant-preferences/{owner_user_id}`;
- `PUT /assistant-preferences/{owner_user_id}`;
- `POST /assistant-preferences/{owner_user_id}/compile`;
- `POST /assistant-preferences/{owner_user_id}/approve`.

Every success and bounded failure response is private and `no-store`.

## Authority boundary

Owner-authored `preference_narrative` is untrusted data. It is never inserted
directly into an answer prompt. The compiler can emit only four presentation
enums and fifteen fixed response-rule identifiers. It cannot:

- weaken safety or factual standards;
- force agreement;
- expose hidden prompts;
- override medical, legal, or other controlling domain policy;
- control tools, retrieval, memory, databases, or external actions;
- create personal facts, philosophy, diagnoses, or goals.

Unsupported or prohibited requests are recorded as bounded rejection codes.
The OpenAI call uses Structured Outputs, zero retries, a 20-second timeout,
`store=false`, and a one-way pseudonymous safety identifier. An empty narrative
uses a deterministic no-provider clear candidate.

## Response projection boundary

Only `effective_assistant_preference_plan_v1` can influence answer presentation.
The projection contains the owner binding, source revision, optional approved
compilation-plan hash, four typed enums, fixed rule IDs, and its own canonical
hash. It contains no free-form narrative, nickname, occupation,
`more_about_you`, assistant name, memory claim, RAG result, tool instruction,
or external-action authority.

The rendered section explicitly remains below safety, factual standards,
controlling domain policy, the current request, and backend-owned response
policy. It is part of the system prompt, not a reference-data block, because it
is approved presentation policy rather than retrieved content. Zep memory,
attachments, Relational Monism, and prior web provenance retain their separate
lower-authority data blocks. An owner with no stored preference row receives no
preference section, preserving the existing default prompt exactly.

## Database boundary

The capability uses the backend's direct PostgreSQL connection to the
transitional platform database. It does not expose `user_settings` through the
Supabase Data API and grants no table access to `anon`, `authenticated`, or
`service_role`. Supabase is the request identity authority; PostgreSQL is the
preference data authority.

The baseline preserves compiler versions v1 through v3 so the nineteen existing
candidate rows remain valid migration inputs. New candidates use v3. The
baseline is for clean disposable databases; it is not permission to execute SQL
on production and does not claim to reconcile an arbitrarily drifted existing
schema.

## Candidate verification

The 76 focused preference/prompt/orchestration/composition tests pass. The
complete sealed-runtime backend suite passes 1,171 tests. Application import
retains 143 routes, 129 OpenAPI paths, four preference operations, and OpenAPI
SHA-256 `199ea9bc499ad8a57e69fdb093c77ad24383389105b76dda9fc0f695b5234b82`.
Static boundary review finds no narrative or profile field reference in the
conversation router, command, orchestrator, or prompt assembler. A live-row
projection was not claimed because the managed database exposes no
administrative owner-enumeration credential to this candidate workflow.

## Activation boundary

This batch restores source, tests, schema source-of-truth, and application
mounting only in the cleanup candidate. It does not:

- change or restart production;
- execute the SQL baseline;
- migrate or rewrite the twenty existing rows;
- deploy the paired frontend or backend.

Before activation, a disposable database must prove the baseline, forced RLS,
cross-owner denial, optimistic conflicts, compilation expiry, approval
atomicity, and preservation of the exact live row counts. The complete backend
suite must prove default-prompt parity, exact owner binding, hash-tamper denial,
and absence of raw narrative/profile text from prompt and provider artifacts.
Production deployment must then verify the authenticated frontend flow and one
approved preference response without exposing prompt contents in logs or public
inspection output.

## Rollback

Before deployment, rollback is deletion of the candidate branch/worktree only.
After a future deployment, rollback is a Git revert of the capability mount and
files; the two retained tables and their rows stay intact. No rollback drops,
truncates, rewrites, or reclassifies user data.
