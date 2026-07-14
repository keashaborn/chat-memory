# Memory V1 review/apply production-clone rehearsal — 2026-07-13

## Boundary

The rehearsal used a separate Docker Postgres project on seebx. Production was
read only while creating the backup and running final verification queries. No
production schema, row, Qdrant collection, prompt path, candidate, preference,
or project record changed.

The rehearsal applied, in order:

1. Preference/project staging from isolated commit `9304c36`.
2. Controlled review/apply from isolated commit `8175781`.

## Source backup

- Path: `/home/ubuntu/brains/snapshots/memory_pre_preference_project_rehearsal_20260713T005247Z.dump`
- Format: PostgreSQL custom archive
- Bytes: `72,922,800`
- Mode: `600`
- SHA-256: `140a54c04f60d02cce93f0da33a5598f0ba3782b22bf2cea89bffe92b33bd4a7`
- Restore catalog: validated with `pg_restore --list`

The archive was restored into Docker project `memoryv1prodclone8175781` with
the production object-owner roles recreated using their locked attributes.

## Restored baseline

The clone reproduced the live Memory V1 state:

- Evidence: `173`
- Active preferences: `0`
- Claims: `6`
- Candidates: `7`
- Evidence ingest batches: `1`
- Artifacts: `5`
- Artifact sections: `213`
- Memory tables: `21`

The preference/project staging and review/apply tables were absent before the
rehearsal. This satisfied the migration's zero-row legacy-preference
precondition.

## Migration and test result

Both migrations applied twice without duplicating objects or changing real
data. The staging SQL test and the review/apply SQL test passed; every synthetic
row was enclosed in a transaction and rolled back.

After migration:

- Memory tables increased from `21` to `39` by exactly the expected `18` tables.
- All `18` tables had enabled and forced RLS.
- All new candidate, review, revision, registration, and apply tables contained
  `0` real rows after test rollback.
- Existing counts remained `173 evidence / 0 preferences / 6 claims / 7
  candidates / 1 ingest batch / 5 artifacts / 213 artifact sections`.
- `memory_review_maintainer` remained `NOLOGIN`, `NOSUPERUSER`, `NOCREATEDB`,
  `NOCREATEROLE`, `NOINHERIT`, and `NOBYPASSRLS`.
- All five backend API functions were owned by
  `memory_review_maintainer`, were `SECURITY DEFINER`, fixed `search_path` to
  `pg_catalog`, and forced `row_security=on`.
- `brains_app` retained preference `SELECT` and controlled function execution,
  while direct preference insert/update, direct review insert, and direct
  revision insert were all denied.

The post-migration schema-only artifact is:

- Path: `/home/ubuntu/memory-v1-reviews/MEMORY_V1_REVIEW_APPLY_PROD_CLONE_SCHEMA_20260713.sql`
- Bytes: `216,279`
- Mode: `600`
- SHA-256: `5a96c722a5f97bafee5d98a98a21254590479470662f98af6ce945ff4adb195a`

## Rollback result

The controlled review/apply rollback and then the staging rollback both passed
against the production-shaped clone. The clone returned to exactly `21` Memory
V1 tables, all new table names became absent, and all baseline data counts were
unchanged.

The disposable clone and its volume were removed after verification. The
validated source backup and schema-only audit artifact were retained.

## Final production verification

Production still had exactly `21` Memory V1 tables; all preference/project
staging and review/apply tables remained absent. Counts remained `173 evidence /
0 preferences / 6 claims / 7 candidates / 1 ingest batch / 5 artifacts / 213
artifact sections`. The Brains service was active, and Postgres, Qdrant, Redis,
and the vetting worker remained running.

## Stop condition

Production compatibility is verified. This rehearsal does not authorize a
production installation. The next operation would require separate approval to
take a fresh backup, apply only the two migrations, run a transactionally
rolled-back live security test, verify zero data deltas, and stop before any
candidate extraction or review persistence.
