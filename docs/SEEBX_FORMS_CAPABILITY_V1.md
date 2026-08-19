# SeeBx Forms capability v1

Status: candidate only; default off; not deployed.

## Purpose

`seebx.capabilities.forms` replaces the unmounted legacy forms router with one
owner-bound, versioned forms capability. It preserves the six contracts used by
the LifeSwitch frontend without restoring the legacy router or its platform
database dependency.

## Canonical route surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/forms/publish` | Create a template or publish its next immutable version |
| `GET` | `/forms/templates/{owner_user_id}` | List the actor's templates |
| `GET` | `/forms/versions/{version_id}` | Read one actor-owned version |
| `POST` | `/forms/entries` | Validate and record one actor-owned entry |
| `GET` | `/forms/entries/list` | List actor-owned entries using bounded filters |
| `DELETE` | `/forms/templates/{owner_user_id}/{template_id}` | Delete one actor-owned template and its dependent records |

No second forms router is permitted. The historical `rag_engine/forms_router.py`
is absent from the candidate tree; its original path is retained only in the
immutable retirement history. `sql/vb_forms_v1.sql` remains temporarily as the
historical source-schema contract and disposable migration fixture. Retiring
that SQL artifact requires accepted production migration and retention evidence.

## Authority chain

1. The browser presents a current Supabase access token to the Verbal Sage BFF.
2. Verbal Sage independently verifies the LifeSwitch product session and tier.
3. The BFF forwards the original bearer token, its service credential, and the
   verified actor identifier to SeeBx.
4. SeeBx independently verifies the request actor using
   `seebx.core.identity.require_actor` or `require_request_actor`.
5. `connect_lifeswitch` starts an application transaction and binds
   `app.user_id` to that verified UUID.
6. PostgreSQL forced row-level security applies the same UUID to every read and
   write in `lifeswitch_forms`.

A path, request body, query parameter, frontend label, service credential, or
database connection alone is not user authority. Owner disagreement fails
closed before application data access.

## Storage boundary

The canonical objects live in the isolated LifeSwitch PostgreSQL database:

- `lifeswitch_forms.form_template`
- `lifeswitch_forms.form_version`
- `lifeswitch_forms.form_entry`

The schema migration is `ops/sql/20260819_lifeswitch_forms_v1.sql`. All owner
columns are UUIDs. Composite owner foreign keys prevent a version or entry from
crossing owners. All three tables have enabled and forced RLS. Only
`lifeswitch_app` and `lifeswitch_owner` receive table access; `PUBLIC` is
revoked. No `anon` or `authenticated` Data API grants are part of this design.

JSON Schema Draft 2020-12 validation is mandatory. Publishing fails if the
schema is invalid or the validator is unavailable. Entry creation fails unless
its object validates against the exact stored version.

## Legacy-data disposition

The read-only legacy inventory found:

| Object | Rows | Distinct owners |
|---|---:|---:|
| Templates | 12 | 2 |
| Versions | 13 | 2 |
| Entries | 313 | 2 |

The hash-bound planner was run twice against the production source on
2026-08-19. Both repeatable-read runs returned the same disposition and hashes:

| Object | Source | Eligible | Quarantine |
|---|---:|---:|---:|
| Templates | 12 | 8 | 4 |
| Versions | 13 | 9 | 4 |
| Entries | 313 | 311 | 2 |

Four templates have non-UUID owner identifiers. Their four dependent versions
are therefore ineligible. Two entries have non-UUID owners and also depend on
ineligible versions. The exact quarantine contains 10 rows; cascading reason
codes do not represent additional rows.

- source bundle SHA-256:
  `397d4205359df2723ebfa3d8eae57fceb48c244952680a1dc6dcad284488b9dd`;
- eligible bundle SHA-256:
  `de1e805193453dd069a36d859db64369feed5f6c2c1c4a6286c4308178cd62ae`;
- quarantine SHA-256:
  `8a696d35401f8fee761528628a822da9eae1d0931836a5ac7e55b6b5fda420c5`.

Both plan outputs reported `production_writes: 0`. These hashes describe the
current source state only; any source change requires a new read-only plan and
new authorization. The production migration must:

1. freeze a repeatable-read export and record source database, table counts,
   primary-key sets, and SHA-256 hashes;
2. import only rows whose owner is a valid UUID and whose complete owner-bound
   foreign-key chain is valid;
3. preserve all 10 ineligible rows in an encrypted, access-controlled,
   hash-bound quarantine artifact without activating them;
4. compare source-eligible and destination primary-key sets exactly;
5. prove cross-owner reads and writes fail through both the API and PostgreSQL;
6. keep the legacy source read-only until the acceptance receipt and rollback
   evidence are approved.

The candidate does not perform or authorize this production migration.
`scripts/migrate_lifeswitch_forms_v1.py` implements the migration gate. Its
default mode is read-only and returns only counts and hashes. Apply mode
requires the exact source, eligible, and quarantine hashes from a prior plan,
a separate approval identifier, encrypted-artifact evidence, and a new output
directory. It transfers eligible rows insert-only inside one serializable
destination transaction through a non-superuser, non-`BYPASSRLS` login that
inherits the `lifeswitch_app` application role. The gate rejects owner-role
membership and elevated role attributes (`CREATEROLE`, `CREATEDB`, and
`REPLICATION`), rejects identical source/destination fingerprints, verifies
the exact destination bundle, and preserves invalid rows in a mode-`0600`
quarantine artifact.

## Activation gate

`SEEBX_FORMS_ENABLED` defaults to false. Activation requires all of the
following evidence in one change record:

- migration and rollback applied successfully to disposable databases;
- exact accepted-row identity and count parity;
- invalid-owner quarantine hash and access location;
- frontend bearer-forwarding candidate deployed and authenticated;
- owner A/owner B isolation tests at HTTP and SQL layers;
- no plaintext secrets in source, logs, receipts, or artifacts;
- candidate Git commits pushed and bound to the deployment receipt;
- production snapshot and rollback commands verified;
- separate approval to apply the schema, migrate data, and enable the flag.

Passing candidate tests is not activation authority.

## Rollback

Before activation, rollback is removal of the candidate flag/configuration;
production behavior is unchanged because the router is not mounted.

After an approved production migration, rollback is:

1. set `SEEBX_FORMS_ENABLED=0` and restart only the SeeBx service;
2. verify the six `/forms` routes are absent and core health remains green;
3. retain the destination database for investigation unless deletion is
   separately authorized;
