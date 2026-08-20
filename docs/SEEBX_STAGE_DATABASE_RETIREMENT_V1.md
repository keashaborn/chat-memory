# SeeBx staging database retirement gate v1

Status: read-only classification complete; full backup and drop not authorized

## Exact target

- Host: SeeBx backend
- Container: `brains-postgres-1`
- Database: `lifeswitch_training_family_stage_20260727`
- Size: 15 MB
- Schemas: `catalog_dev`, `lifeswitch_training`, `public`
- Application tables: 29
- Active connections at the 2026-08-20 audit: 0
- Source, environment, service, timer, and configuration references: 0

This gate never targets the live `memory` database, the canonical
`lifeswitch-postgres-current` container, or any schema inside the canonical
`lifeswitch` database.

## Data-preservation audit

The existing archive
`/home/ubuntu/archive-staging/lifeswitch-memory-retirement-20260817T111218Z/retired-memory/postgres/legacy-lifeswitch_training_family_stage_20260727-schema.pgcustom`
is schema-only. It is 176,657 bytes with SHA-256
`7ae04c52b2738585e67390f75d071537f49e2d6b0aaf85fa162e440ef8be31c5`
and contains no `TABLE DATA` entries. It is not sufficient recovery evidence
for a database containing training history.

A content-free row comparison against the canonical `lifeswitch` database
found exact inclusion for 25 of 29 tables. Four differences were classified:

| Table | Verified explanation |
|---|---|
| `catalog_dev.exercise_family` | 28 source and 28 target rows; UUIDs were regenerated, while the semantic projection has identical SHA-256 `132ec72f681b0bc1082efca30895cc22221e5633a8d4f7a5c66ce9005d74b8cc` |
| `catalog_dev.exercise_family_member` | 131 source and 131 target rows; member/family UUIDs were regenerated, while the family-slug/exercise semantic projection has identical SHA-256 `f491c24f90aacd7ff142b0c3f9fe2b9f67776b6004514341f387d7c2796a52fc` |
| `lifeswitch_training.workout_template` | All 16 source primary keys exist in the canonical database; four rows have later `updated_at` values and one of those has a later name |
| `lifeswitch_training.workout_template_share` | All seven source primary keys exist; four rows have later revocation status/timestamp and rotated token hashes in the canonical database |

The staging database is therefore an older migration source, not a current
authority. The comparison does not authorize deletion.

## Required recovery gate

Before a drop authorization is requested:

1. Create a full custom-format dump containing schema and data in a new
   root-owned mode-`0700` backup directory on encrypted storage.
2. Record the dump byte count and SHA-256 without recording row values.
3. Restore the dump into one uniquely named disposable database.
4. Verify all 29 application tables, row counts, constraints, functions, and
   row fingerprints against the source.
5. Drop the disposable database and prove its absence.
6. Write a content-free receipt binding the source database identity, dump,
   verifier, restore result, and exact rollback command.
7. Preserve the receipt and dump before requesting a separately authorized
   exact database drop.

Failure at any step leaves the source database unchanged and blocks retirement.

## Later drop boundary

The later authorization must name only
`lifeswitch_training_family_stage_20260727`, verify zero connections again,
bind the full-dump and restore-receipt hashes, execute one exact database drop,
and confirm that both PostgreSQL containers plus authenticated LifeSwitch
training reads remain healthy. It must not be combined with the Zep cutover,
legacy-memory schema retirement, Redis stop, credential rotation, or service
deployment.
