# Memory V1 preference/project production installation — 2026-07-13

## Authorization and boundary

The user separately authorized a fresh production backup, installation of only
preference/project staging `9304c36` and controlled review/apply `8175781`,
transactionally rolled-back live security tests, and zero-data-change
verification. Candidate extraction, review persistence, preference/project
application, Qdrant writes, and prompt/runtime changes were explicitly outside
this operation.

The migrations and tests ran on seebx from the clean isolated worktree
`/home/ubuntu/chat-memory-memory-v1`. The unrelated production worktree
`/opt/chat-memory` remained on `catalog_exercise_holds_defaultlog_v0` at
`78906f8` and was not modified.

## Verified inputs

- Staging SQL SHA-256:
  `ca899c086ab5a66770eb7b39f34421101527716467af854fae7e9cb77acea1d7`
- Staging test SHA-256:
  `55b6360a43b983ab9f73a05859ffc346922976a60b3309db3a5f21dd73159ccd`
- Review/apply SQL SHA-256:
  `72a69782889fe62fe6fd7c50e8cc62eaff1256eaebee6b330656912bc14579b5`
- Review/apply test SHA-256:
  `842fe8ab640324853e61c72ff73fd3f09ed5e5beeffba4cc4d5cc94103cfb3c9`

Every checksum was verified immediately before execution.

## Fresh restore point

- Path:
  `/home/ubuntu/brains/snapshots/memory_pre_preference_project_install_20260714T010425Z.dump`
- Format: PostgreSQL custom archive from a serializable-deferrable snapshot
- Bytes: `72,942,172`
- Mode: `600`
- Restore-catalog entries: `909`
- SHA-256:
  `c45d2a58789808b43f659403a04a57958779e7e672b3f90d28348d25ac7d491d`

`pg_restore --list` validated the archive before either migration ran.

## Baseline and installation

The pre-install live state was `21` Memory V1 tables, `173` evidence rows, `6`
claims, `7` candidates, `0` preferences, `1` evidence-ingest batch, `5`
artifacts, `213` artifact sections, and `0` evidence-lifecycle events. All 18
target tables and `memory_review_maintainer` were absent.

Staging `9304c36` committed first. Its synthetic owner-isolation, append-only,
evidence-link, review, replacement, and lifecycle tests completed inside
`BEGIN`/`ROLLBACK`; all staging tables remained empty. Review/apply `8175781`
then committed. Its review sequencing, request replay, controlled apply,
cross-owner denial, job-authority, stale-revision, and append-only tests also
completed inside `BEGIN`/`ROLLBACK`.

The final schema has `39` Memory V1 tables: the original 21 plus the expected
18 preference/project candidate, review, revision, registration, relation, and
apply-event tables.

## Live security result

- All 18 new tables have RLS enabled and forced.
- All 18 new tables contain zero rows after both test rollbacks.
- `memory_review_maintainer` is no-login, non-superuser, cannot create roles or
  databases, and cannot bypass RLS.
- Thirteen maintainer-owned functions are private guards/hash helpers or
  controlled APIs. No function is executable by `PUBLIC`.
- Only the five intended review/apply/register entry points are executable by
  `brains_app`; they are `SECURITY DEFINER` with fixed
  `search_path=pg_catalog` and `row_security=on`.
- `PUBLIC` has zero privileges on the 18 new tables.
- The new schema has zero Vantage columns, and the controlled function bodies
  have zero Vantage references.

## Zero-data-change proof

The fresh pre-install archive was restored into temporary database
`memory_verify_20260714t010425z`. For every one of the 21 original Memory V1
tables, a manifest compared both row count and an order-independent canonical
hash of every JSON row. The pre-install restored database and post-install live
database matched for all 21 tables with no diff.

Key post-install counts remain `173 evidence / 6 claims / 7 candidates / 0
preferences / 5 artifacts / 213 artifact sections`. All new preference and
project durable/candidate/apply counts are zero. The temporary verification
database was removed and its absence was confirmed.

## Service result and stop condition

Brains remained active. The production Postgres, Qdrant, Redis, and vetting
worker containers remained running. The production Git worktree was unchanged.

This operation stops before candidate extraction or persistence. It does not
authorize writing the reviewed preference/project drafts, reviews, durable
preferences, project knowledge, Qdrant projections, or prompt inputs.
