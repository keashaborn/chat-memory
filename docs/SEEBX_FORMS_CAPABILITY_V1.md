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
remains unmounted and is a retirement target after migration evidence is sealed.

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

Two entry rows use non-UUID owner identifiers. The production migration must:

1. freeze a repeatable-read export and record source database, table counts,
   primary-key sets, and SHA-256 hashes;
2. import only rows whose owner is a valid UUID and whose complete owner-bound
   foreign-key chain is valid;
3. preserve the two invalid-owner rows in an encrypted, access-controlled,
   hash-bound quarantine artifact without activating them;
4. compare source-eligible and destination primary-key sets exactly;
5. prove cross-owner reads and writes fail through both the API and PostgreSQL;
6. keep the legacy source read-only until the acceptance receipt and rollback
   evidence are approved.

The candidate does not perform or authorize this production migration.

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
