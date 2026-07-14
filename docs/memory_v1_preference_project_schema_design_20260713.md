# Memory V1 preference and project-knowledge schema — 2026-07-13

## Authority and scope

The user ratified the 72 specialized-unit classifications as design input on
2026-07-13. This phase defines and tests database structure only. It does not
apply the migration to production, extract proposals, persist reviews, change
active preferences, create project knowledge, retrieve records, write Qdrant,
or alter prompts.

Claims, preferences, and project knowledge remain separate lanes. The existing
`memory.candidate` table remains claim-specific. No generic cross-target
candidate table is introduced.

## Preference lane

`memory.preference_candidate` stores immutable, hash-deduplicated proposals.
Each proposal distinguishes response from life preferences and records domain,
key, JSON value, polarity, scope, explicitness, stability, surface policy,
extraction confidence, sensitivity, and extractor version.

`memory.preference_candidate_evidence` links one or more immutable evidence
rows. Only active evidence can be linked. `memory.preference_candidate_review`
stores numbered, append-only decisions bound to the exact candidate SHA-256.
Rewrite and split lineage is represented by
`memory.preference_candidate_review_replacement`.

The current `memory.user_preference` table is the eventual active-state target,
but `brains_app` now receives SELECT only in this migration. No reviewed apply
function exists yet, so neither staging nor review can change active response
behavior.

## Project-knowledge lane

`memory.project_space` establishes an immutable owner-scoped project identity.
Project proposals, evidence, reviews, and rewrite/split lineage use dedicated
tables matching the preference workflow.

The durable store separates stable identity from versioned content:

- `memory.project_knowledge_head` identifies a project knowledge key and kind;
- `memory.project_knowledge_revision` stores numbered immutable text revisions,
  content hashes, document state, authority level/source, effective time,
  sensitivity, accepted review, and revision ancestry;
- `memory.project_knowledge_revision_evidence` preserves evidence provenance;
- `memory.project_knowledge_relation` records supersession, conflict,
  qualification, dependency, and derivation between knowledge heads.

Revision number is not authority. Newer text does not automatically supersede
older text. Authority, document state, effective time, explicit relations, and
evidence remain independently queryable.

## Security and mutation boundary

All 13 new tables use the authenticated Supabase UUID as `owner_user_id`,
composite owner foreign keys, enabled and forced RLS, and an owner-first index
for owner-filtered access. Vantage/persona identifiers do not appear in any
ownership key or filter.

`brains_app` may SELECT and INSERT only the four proposal/evidence staging
tables. It has SELECT only on review, project registry, and durable project
tables. It cannot update or delete any new table, append a review, create a
project, write a durable revision, or mutate `memory.user_preference`.

Database triggers reject UPDATE and DELETE even for the table owner. Evidence
link triggers run under the locked, non-login, non-bypass-RLS evidence
maintainer and reject non-active or cross-owner evidence. Review foreign keys
bind every decision to the exact candidate hash. A separate locked, non-login,
non-bypass-RLS review maintainer verifies that review-time evidence is still
active and serializes against evidence redaction. Replacement guards require a
rewrite or split decision, reject self-reference, and enforce deterministic
ordinals.

Project revision guards require an `accept` review, exact candidate/head key and
kind alignment, exact text/authority/state/effective-time/sensitivity equality,
a database-computed SHA-256, single-use accepted reviews, and a same-head,
gapless revision chain. Evidence lifecycle audit rows count
preference-candidate, project-candidate, and durable project-revision links
separately.

The `memory` schema remains private. No `anon` or `authenticated` Supabase Data
API grants are added.

## Validation

The isolated seebx worktree applies the migration twice, runs tenant-isolation,
privilege, append-only, hash-lock, active-evidence, versioning, and lifecycle
audit tests, executes every existing Memory V1 integration test, dumps the
schema, and runs the guarded rollback. The complete disposable PostgreSQL 16
suite passes.

## Intentionally absent apply authority

Raw review and durable tables have no application write grant. A later,
separately authorized migration must add narrow security-definer functions that
atomically:

1. lock the candidate and active evidence;
2. verify the expected candidate hash and latest review number;
3. require at least one active evidence link;
4. validate rewrite/split replacement semantics;
5. append the review event;
6. for an accepted proposal, compare current target state and append the active
   preference revision or project revision without overwriting history;
7. write a complete owner-scoped audit event and prove idempotent replay.

That future path is the next design step. This migration alone cannot apply the
three reviewed preference drafts or five reviewed project drafts.
