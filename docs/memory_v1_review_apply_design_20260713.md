# Memory V1 controlled review/apply design — 2026-07-13

## Scope

This phase adds database-owned review and apply operations for response/life
preferences and project knowledge. It does not extract new conversations,
promote claims, alter prompt construction, write Qdrant, or change production.
The sole owner and RLS boundary remains the Supabase user UUID in
`app.user_id`; `vantage_id` is absent from every key and function.

## Public backend functions

Only `brains_app` may execute these `SECURITY DEFINER` functions:

- `memory.register_project_space(...)`
- `memory.review_preference_candidate(...)`
- `memory.review_project_candidate(...)`
- `memory.apply_preference_candidate(...)`
- `memory.apply_project_candidate(...)`

Their owner is the locked `NOLOGIN`, `NOINHERIT`, `NOBYPASSRLS`
`memory_review_maintainer` role. Each function fixes `search_path` to
`pg_catalog`, forces row security on, and requires the actor UUID to match the
row owner. The application retains read-only access to reviews, durable
revisions, apply events, project heads, and the active preference snapshot.

## Review contract

The database assigns the next gapless per-candidate review number while holding
an owner/candidate advisory transaction lock. Every call supplies a UUID
`request_id` and expected immutable candidate hash. A canonical SHA-256 of all
semantic inputs makes exact request replay idempotent; reuse with changed input
fails.

The source candidate and every replacement must have at least one evidence link
and all linked evidence must still be active. Evidence rows are locked against
concurrent lifecycle transitions until review completes. Rewrite requires one
replacement, split requires at least two ordered unique replacements, and other
decisions permit none. Jobs may rewrite, defer, or split, but may not accept or
reject. User/admin authorization is enforced by the trusted backend before it
selects the reviewer type; SQL still records reviewer type/ref and the owner
actor.

## Apply contract

Apply accepts only the candidate's latest review when that review is `accept`.
The accepted review can be consumed once. A separate request hash makes exact
apply replay idempotent and rejects changed request reuse. Apply takes an
owner/key advisory transaction lock, rechecks and locks every evidence row, and
requires the caller's expected current revision ID to exactly match the active
state. A stale expected revision fails with SQLSTATE `40001`.

Preference apply appends an immutable `preference_revision`, copies every
candidate-evidence link, appends an apply event, and updates the single active
`user_preference` snapshot in the same transaction. A trigger rejects direct or
non-revision-matched snapshot writes.

Project apply appends a gapless same-head `project_knowledge_revision`, copies
every candidate-evidence link, and appends an apply event. The candidate hash is
the review-bound revision content hash. Unverified document state or authority
cannot be accepted or applied. Newer candidates do not automatically supersede
older revisions; explicit accepted review plus optimistic current-revision
matching is required.

## Audit and immutability

Review requests store their request IDs and hashes. Project registration,
preference apply, and project apply each write owner-scoped append-only events.
Durable revisions and their evidence links are append-only. Physical update or
delete attempts fail. The rollback refuses to run while reviews, durable rows,
events, or active preference rows exist.

## Verification

`tests/memory_v1_review_apply.sql` verifies forced RLS, narrow grants, locked
function ownership/configuration, exact replay, changed-request rejection, job
authority limits, stale revision rejection, two-revision preference and project
chains, unverified-project rejection, cross-owner denial, and append-only
guards. `tools/memory_v1_schema_ci.sh` applies the migration twice, runs this
test plus the full prior Memory V1 suite, then exercises the guarded rollback.
