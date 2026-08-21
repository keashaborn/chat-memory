# SeeBx refactor batch 14: Measurements PostgreSQL effect boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves all four Measurements SQL effects, the
delegated People-permission lookup, and isolated LifeSwitch connection lifetime
from `seebx.capabilities.measurements.routes` into the named
`seebx.adapters.lifeswitch_measurements_postgres` repository. The capability
retains request identity, owner matching, delegated-access policy, input
validation, response serialization, and stable HTTP error mapping.

The previously interpolated `LIFESWITCH_PEOPLE_SCHEMA` value is now admitted
only as one lowercase PostgreSQL identifier and fails before a connection is
opened if invalid. No SQL text or dotted identifier can enter through it.

## Changed files

- `seebx/adapters/lifeswitch_measurements_postgres.py`
- `seebx/capabilities/measurements/routes.py`
- `tests/test_measurements_capability_v1.py`
- `tests/test_lifeswitch_measurements_postgres_adapter_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_14_V1.md`
- current structural, residual, component, and authority ledgers

## Verified results

- Measurements capability database effects decrease from four to zero.
- The named PostgreSQL adapter owns exactly four effects and guarantees close.
- Three frontend-facing route contracts remain unchanged.
- Owner predicates, active-row filtering, the 24-parameter upsert, delegated
  permission scope, deterministic response mapping, and stable 404 are retained.
- All 20 focused Measurements, isolated-database, and identity tests pass.
- All 1,224 locked-runtime backend tests pass.
- Parent and candidate route lists and OpenAPI documents are canonical hash
  matches under the same service environment. With optional routes off, both
  have 138 routes, route SHA-256
  `dacb3665272ec6720fb477340c9d84d977a6af55fae4f061972526eabe10353d`,
  and OpenAPI SHA-256
  `44e8aa5f364f85aef4d4fb4fa2596af4ab3eb219a2a82e21d54fbd2768c71091`.
- Eight capability modules and 198 true database effects remain after excluding
  the non-database LifeSwitch stage `.execute` call.

## Deployment state

Candidate only. No deployment, restart, database write, schema change,
migration, frontend edit, route change, delegated-access activation,
credential change, service change, AWS action, or production-file change is
authorized or performed by this batch.
