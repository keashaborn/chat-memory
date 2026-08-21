# SeeBx refactor batch 15: AI Operations PostgreSQL effect boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves all six AI Operations query and transaction
effects, the actor/capability session settings, connection bounds, and
connection lifetime from `seebx.capabilities.operations.ai_operations` into
the named `seebx.adapters.ai_operations_postgres` repository. The capability
retains UUID/state/limit validation, incident contract validation, stable
public error mapping, and inspector/manager capability selection. The route
owner retains verified actor and authorization enforcement.

The shared `PostgresConnectionProvider` now accepts a copied, validated mapping
of connection options. Existing users retain the exact default behavior; only
the separately composed AI Operations provider supplies the existing
`command_timeout=10` and `timeout=5` bounds.

## Changed files

- `app.py`
- `seebx/adapters/ai_operations_postgres.py`
- `seebx/adapters/postgres.py`
- `seebx/capabilities/operations/ai_operations.py`
- `seebx/capabilities/operations/ai_operations_routes.py`
- `tests/test_admin_ai_operations_v1.py`
- `tests/test_postgres_connection_provider.py`
- `docs/SEEBX_REFACTOR_BATCH_15_V1.md`
- current structural, residual, component, and authority ledgers

## Verified results

- AI Operations capability database effects decrease from six to zero.
- The named PostgreSQL adapter owns exactly six effects and guarantees close.
- List, acknowledge, and resolve SQL are fixed constants; no database function
  identifier is dynamically assembled in capability code.
- Actor and `inspector.view`/`incident.manage` settings remain transaction-local.
- PostgreSQL states `P0002` and `22023` retain the exact 404 and 409 mappings;
  other repository failures remain the stable 500 unavailable response.
- All 17 focused AI Operations and connection-provider tests pass.
- All 1,227 locked-runtime backend tests pass.
- Parent and candidate route lists and OpenAPI documents are canonical hash
  matches under the same service environment. With optional routes off, both
  have 138 routes, route SHA-256
  `dacb3665272ec6720fb477340c9d84d977a6af55fae4f061972526eabe10353d`,
  and OpenAPI SHA-256
  `44e8aa5f364f85aef4d4fb4fa2596af4ab3eb219a2a82e21d54fbd2768c71091`.
- Seven capability modules and 192 true database effects remain after excluding
  the non-database LifeSwitch stage `.execute` call.

## Deployment state

Candidate only. No deployment, restart, database write, schema change,
migration, frontend edit, route change, credential change, service change, AWS
action, or production-file change is authorized or performed by this batch.
