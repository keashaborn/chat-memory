# SeeBx refactor batch 12: canonical USDA provider boundary

Date: 2026-08-20

## Scope

This candidate-only batch creates one canonical USDA FoodData Central adapter
for every retained active consumer. Catalog barcode lookup, catalog guided food
matching, and owner-scoped nutrition import no longer read the API key, build
provider URLs, invoke `requests`, or decode provider failures independently.
The adapter resolves configuration at call time, owns the two USDA endpoints,
normalizes JSON/transport/status failures without leaking credentials or
network details, and centralizes USDA nutrient-number translation.

Catalog scoring and response shaping remain capability logic. Nutrition keeps
owner verification, serving inference, and its isolated PostgreSQL transaction.
The inactive catalog USDA search/import routes remain unchanged until their
separate evidence-bound route-retirement batch; no active caller is moved onto
those legacy surfaces.

## Changed files

- `seebx/adapters/usda_fdc.py`
- `seebx/capabilities/catalog/routes.py`
- `seebx/capabilities/nutrition/routes.py`
- `tests/test_usda_fdc_adapter.py`
- `tests/test_usda_provider_boundaries.py`
- `docs/SEEBX_REFACTOR_BATCH_12_V1.md`

## Required verification

- All three retained active USDA flows use one provider adapter.
- Provider code has no database, owner, FastAPI, or mutation authority.
- Nutrition verifies the owner before provider and database access.
- Barcode and guide response fields and route/OpenAPI contracts remain exact.
- Missing configuration, not-found, non-200, invalid JSON, and transport
  failures produce stable content-free errors.
- Focused tests and the complete locked-runtime backend suite pass.

## Deployment state

Candidate only. No deployment, restart, provider call, database write, route
retirement, security-policy change, credential change, frontend change, or
production cleanup is authorized by this batch.

## Verified results

- Python compilation passes for the adapter, both capability modules, and both
  new test modules.
- All 23 focused USDA, active-route response-contract, catalog-PostgreSQL, and
  HTTP-boundary tests pass.
- All 1,205 locked-runtime backend tests pass.
- The application remains exactly 143 routes and 129 OpenAPI paths.
- The canonical OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- No USDA provider call, database write, service restart, deployment, credential
  change, or production-file change was performed.
