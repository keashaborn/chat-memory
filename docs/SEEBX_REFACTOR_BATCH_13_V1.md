# SeeBx refactor batch 13: Forms PostgreSQL effect boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves every retained Forms SQL statement,
transaction, dynamic query, command-count decision, and isolated LifeSwitch
connection lifetime from `seebx.capabilities.forms.routes` into the named
`seebx.adapters.lifeswitch_forms_postgres` repository. The capability retains
Supabase actor/owner enforcement, UUID and JSON-schema validation, response
serialization, and stable HTTP error mapping.

The six route contracts and the default-off `SEEBX_FORMS_ENABLED` gate are
unchanged. The production Forms schema is still absent. The existing migration
and its valid/quarantine plan are not applied by this batch.

## Changed files

- `seebx/adapters/lifeswitch_forms_postgres.py`
- `seebx/capabilities/forms/routes.py`
- `tests/test_forms_capability_v1.py`
- `tests/test_lifeswitch_forms_postgres_adapter_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_13_V1.md`
- current structural, residual, component, and authority ledgers

## Verified results

- Forms capability database effects decrease from 15 to zero.
- The named PostgreSQL adapter owns exactly 15 effects and guarantees close.
- Publish and delete remain transactional; owner predicates remain in every
  Forms query or mutation; JSON values remain deterministic.
- All 27 focused Forms and migration tests pass.
- All 1,211 locked-runtime backend tests pass.
- Parent and candidate route lists and OpenAPI documents are byte-canonical
  hash matches under the same service environment. With optional routes off,
  both have 138 routes, route SHA-256
  `dacb3665272ec6720fb477340c9d84d977a6af55fae4f061972526eabe10353d`,
  and OpenAPI SHA-256
  `44e8aa5f364f85aef4d4fb4fa2596af4ab3eb219a2a82e21d54fbd2768c71091`.
- Nine capability modules and 202 true database effects remain after excluding
  the non-database LifeSwitch stage `.execute` call.

## Deployment state

Candidate only. No deployment, restart, route activation, schema creation,
migration, database write, data quarantine, frontend change, credential
change, service change, AWS action, or production-file change is authorized or
performed by this batch.
