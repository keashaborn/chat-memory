# Memory V1 Projection Staging Migration V5

Status: executable and clone-verified; not installed in production; runtime inactive.

Server boundary: seebx backend. No Verbal Sage, RESSE, Resse-Train, Qdrant,
retrieval, prompt, timer, or production database behavior is changed.

## Files

- `ops/sql/20260715_memory_v1_projection_staging_v5.sql`
- `ops/sql/20260715_memory_v1_projection_staging_v5_rollback.sql`
- `tests/memory_v1_projection_staging_v5.sql`
- `tools/memory_v1_projection_staging_v5_production_clone.sh`

## Prerequisites and repository gap

The migration fails closed unless the V5 observation/writer schema and the
deployed durable claim, preference, and project schemas already exist. It does
not reconstruct or guess missing durable schemas.

Production contains the older preference/project durable tables, but their
migrations (`9304c36` and `8175781`) are not ancestors of the isolated
`memory_v1_extraction_v2` branch. Therefore this phase is verified against a
read-only production-schema clone. A clean repository reconstruction is not
claimed, and no obsolete candidate pipeline was copied into V5.

## Staging model

The migration adds eleven owner-scoped append-only tables:

```text
projection_plan
projection_plan_item
projection_claim_payload
projection_preference_payload
projection_project_payload
projection_plan_observation
projection_plan_relation
projection_review
projection_apply_event
preference_revision_observation
project_knowledge_revision_observation
```

`projection_plan` stores the exact packet text, raw text hash, canonical packet
hash, owner-bound manifest, projector identity, and item count.
`projection_plan_item` stores the exact typed projection, server-recomputed
semantic identity, target action and revision lock, temporal handling, and
review state. The three payload tables have real typed foreign keys; there is
no weak polymorphic target ID.

Every observation link binds owner, observation ID, and observation hash. A
new composite unique key on `memory.observation` makes that identity enforceable
by a foreign key. Supporting/context observations must match the plan's bound
entities, predicate, polarity, modality, projection class, and surface policy.

## Database enforcement

- The packet and every projection are closed, hash-locked structures.
- The database recomputes canonical JSON hashes and owner manifests.
- Semantic identity includes owner, lane, proposition, polarity, modality, and
  typed lane scope; time is excluded.
- Plan count, exact packet membership, exactly one typed payload, exact
  observation membership, and exact relation membership are deferred
  constraint-trigger checks.
- Corrections require a reviewed `corrects` or `supersedes` relation.
- Response preferences require `zero_token_control_only`.
- Project knowledge requires a real owner-scoped project and
  `exact_project_scope_only`.
- Existing-target actions require a target ID and optimistic revision number.
- All new foreign keys are owner-prefixed except the global predicate registry.

The maintenance role `sage` owns the tables. The restricted `NOLOGIN`,
`NOINHERIT`, `NOBYPASSRLS` role `memory_v5_writer` receives only `SELECT` and
`INSERT`. Every table has enabled and forced RLS with a policy explicitly
targeted only to `memory_v5_writer`. Every insert also passes an actor trigger.
Update and delete are denied by privilege and by append-only triggers.
`brains_app` is not a member of the writer role and has no direct table or
internal-helper access.

This migration deliberately stages plans and provenance only. It creates no
public or application apply API and writes no claim, preference, project
revision, vector, outbox, retrieval, or prompt record.

## Clone security suite

The runner reads production with `pg_dump --schema-only --no-owner
--no-privileges`, restores it into a disposable PostgreSQL 16 container, and
then:

1. Applies the V5 relational staging and writer prerequisites twice.
2. Runs both prerequisite security suites.
3. Applies the projection migration twice.
4. Runs one rolled-back synthetic packet containing a claim, response
   preference, and exact-project-scoped requirement.
5. Tests canonical hashes against Python contract vectors.
6. Tests forced RLS, exact policy roles, direct application denial, missing
   actor denial, cross-owner denial, duplicate replay, append-only denial,
   typed-payload completeness, and zero durable writes.
7. Proves all fixture and projection tables are empty after rollback.
8. Runs the guarded projection, writer, and relational rollbacks.
9. Proves the baseline predicate count and absence of all V5 objects.
10. Removes the container and volume.

Verified result on 2026-07-15:

```text
memory_v1_relational_staging_v5: PASS
memory_v1_relational_writer_v5: PASS
memory_v1_projection_staging_v5: PASS
memory_v1_projection_staging_v5_production_clone: PASS
```

Production was read only. No production installation, rows, schema, timer,
Qdrant collection, retrieval path, or prompt contributor changed.

## Guarded rollback

The rollback must run as `sage` and refuses to proceed if any projection or
provenance table contains a row. It removes only objects introduced by this
migration and revokes only its durable-target read grants. It is a clone and
pre-activation rollback, not a deletion mechanism for live projection data.

## Next boundary

Design controlled preflight/review/apply functions that lock the exact plan,
revalidate active evidence and owner-scoped targets, write exactly one typed
durable revision plus observation links, and record an idempotent apply event.
That phase remains separate and must not activate retrieval or production
installation.
