# SeeBx normalized muscle capability v1

Date: 2026-08-22

Status: isolated candidate implementation; no production deployment, database
mutation, route activation, array retirement, or rule-table retirement authority

## Canonical authority

The capability has one product-data flow:

`catalog_dev.muscle` and `catalog_dev.muscle_alias`
→ `catalog_dev.exercise_muscle`
→ `PostgresLifeSwitchCatalogReader`
→ versioned catalog contracts
→ `/catalog` product routes.

The canonical identity is `muscle_slug`. `display_name`, localized aliases,
parent identity, and region are descriptive fields. An alias such as `Chest`
does not create a second muscle identity.

## Candidate routes

- `GET /catalog/muscles` returns
  `seebx_normalized_muscle_catalog_v1`. It supports bounded query, region,
  locale, and limit inputs.
- `GET /catalog/exercises/{exercise_id}/muscles` returns
  `seebx_exercise_muscle_profile_v1` for an active public exercise.

The exercise profile returns canonical mappings with role, weight, hierarchy,
and localized aliases. Roles are limited to `primary`, `secondary`, and
`stabilizer`. Invalid identities, duplicate relationships, absent weights, and
invalid contract rows fail closed.

## Compatibility boundary

`compatibility_primary_muscles` and `compatibility_secondary_muscles` are
deterministic projections from the normalized relationships. They are not read
from the legacy arrays. The response explicitly declares:

- `normalized_relationships_authoritative=true`;
- `legacy_arrays_authoritative=false`.

The existing `/catalog/exercises/browse` family and variant
`primary_muscles` fields are also projected from `exercise_muscle`; the
candidate browse query no longer reads either stored primary-muscle array.

The existing database arrays remain physically present until all 188 exercises
are reviewed, the 34 currently unmapped exercises are curated, and frontend
parity plus rollback are proven. This candidate does not dual-write either
representation.

## Security and verification

- both adapter operations use read-only PostgreSQL transactions;
- exercise profiles require `is_active=true` and `is_public=true`;
- SQL contains no mutation operation;
- exact SQL prepared and executed as `lifeswitch_app_login` with zero-row probes
  inside a repeatable-read, read-only transaction, then rolled back;
- eight focused capability tests, seven existing catalog adapter tests, and two
  exact route-inventory tests pass;
- the complete candidate suite passes 1,376 tests;
- production source, environment, database, routes, and services are unchanged.

The next authority gate is a disposable Work Runner database test with the
clean baseline, representative canonical mappings, teardown proof, and no
external network or production credentials.
