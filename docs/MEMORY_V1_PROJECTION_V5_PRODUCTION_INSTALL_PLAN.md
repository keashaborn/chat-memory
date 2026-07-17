# Memory V1 V5 controlled production installation

Status: designed and clone-tested; production installation is not authorized.

Target: **seebx backend**, PostgreSQL 16 container `brains-postgres-1`, database
`memory`. No command in this plan runs on Verbal Sage, RESSE, or Resse-Train.

## Boundary

This phase may install only the five hash-locked V5 schema/function migrations,
seed the proposed inactive predicate registry, and run the four hash-locked SQL
security suites whose synthetic writes are enclosed by `BEGIN`/`ROLLBACK`.

It must stop before live extraction, entity resolution, review, projection,
durable apply, retrieval, prompt influence, Qdrant/Redis writes, service restart,
worker installation, or timer activation.

## Two-artifact authorization

The committed plan is:

`ops/manifests/memory_v1_projection_v5_production_install_plan_20260715.json`

It permanently contains `"production_authorized": false`. Editing this value to
`true` invalidates verification; it is not an authorization mechanism.

After separate approval, create one short-lived authorization JSON outside the
Git worktree with mode `0600`. It must contain exactly:

```json
{
  "contract_version": "memory_v1_projection_v5_production_install_authorization_v1",
  "authorization_id": "UUID",
  "authorized": true,
  "authorized_by": "Eric Lund",
  "authorized_at": "UTC ISO-8601 timestamp",
  "expires_at": "UTC ISO-8601 timestamp no more than 30 minutes later",
  "plan_sha256": "canonical plan SHA-256",
  "expected_head_commit": "exact 40-character Git commit",
  "target_server": "seebx"
}
```

The verifier rejects an extra/missing field, mode other than `0600`, stale time
window, wrong plan hash, wrong commit, or wrong server.

## Installation sequence

1. **seebx backend — read-only preflight.** Verify the plan contract, source
   hashes, required Git ancestor, PostgreSQL 16, maintenance role, required base
   tables/extensions, absence of all V5 tables and `memory_v5_writer`, backup
   tools, backup directory permissions, and at least twice the database size in
   free space.
2. **Hard approval boundary.** Create the short-lived authorization only after
   reviewing the preflight. The authorization is not committed.
3. **seebx backend — transaction-consistent backup.** Create a custom-format
   `pg_dump`, require nonzero bytes, generate a `pg_restore -l` catalog, calculate
   SHA-256, and set all evidence files to mode `0600`. No schema write may occur
   before this completes.
4. **Recheck authorization.** A backup that outlives the authorization window
   cannot cross the schema-write boundary.
5. **Install schema/functions only.** Apply the five migrations in manifest order
   with `ON_ERROR_STOP`, a 5-second lock timeout, and a 120-second statement
   timeout. Each migration owns its transaction.
6. **Run rolled-back security tests.** Run the four suites in manifest order.
   Their synthetic accounts and memory rows are rolled back. Assertions after
   `ROLLBACK` prove zero residual test data.
7. **Postflight.** Prove every preexisting `memory` table count except global
   predicate reference data is byte-for-byte unchanged; every V5 owner/staging
   table is empty; `memory_v5_writer` is `NOLOGIN NOINHERIT NOBYPASSRLS`; and
   `brains_app` has no direct V5 table privileges.
8. **Registry check.** Require exactly 44 contracts in
   `memory_predicate_registry_v5`, the locked registry hash, status `proposed`,
   and `runtime_active=false`.
9. **Hard stop.** Write the install report and checksum. Do not invoke extraction,
   projection staging, review, apply, retrieval, workers, timers, or restarts.

## Later production command

Run only after the separate authorization exists.

```bash
# Server: seebx backend
cd /home/ubuntu/chat-memory-extraction-v2
MEMORY_V1_PRODUCTION_INSTALL=authorized \
  tools/memory_v1_projection_v5_production_install.sh \
  ops/manifests/memory_v1_projection_v5_production_install_plan_20260715.json \
  /home/ubuntu/brains/authorizations/SHORT_LIVED_AUTHORIZATION.json
```

The runner takes an exclusive host lock. A failure stops immediately and retains
the backup and phase-status evidence. It does not attempt an implicit broad
rollback after a partially completed migration sequence; recovery must be chosen
from the captured backup and exact failure phase after review.

## Supabase/Postgres posture

The `memory` schema remains private and outside the client Data API. Application
access is function `EXECUTE` only; V5 tables use forced RLS and a restricted
`NOLOGIN` writer. The production database remains pinned to its current
PostgreSQL 16 major during this installation. Unrelated self-hosted Supabase
PostgreSQL defaults are not adopted as part of a memory-schema change.
