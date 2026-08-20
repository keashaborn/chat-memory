# SeeBx refactor batch 11: isolated exercise catalog reader

Date: 2026-08-20

## Scope

This candidate-only batch moves the two active exercise catalog queries out of
the catalog HTTP module and into one read-only PostgreSQL adapter. The adapter
resolves only `LIFESWITCH_POSTGRES_DSN`, fixes the canonical schema to
`catalog_dev`, owns connection lifecycle, and executes every query inside an
explicit read-only transaction. HTTP validation, normalization, aggregation,
response fields, route methods, and route paths remain in the capability.

The five inactive catalog route candidates, USDA provider code, food mutation
code, HTTP authentication boundary, database contents, ACLs, and production
configuration are intentionally unchanged.

## Changed files

- `seebx/adapters/lifeswitch_catalog_postgres.py`
- `seebx/capabilities/catalog/routes.py`
- `tests/test_lifeswitch_catalog_postgres.py`
- `tests/test_lifeswitch_training_exercise_family_catalog.py`
- `docs/SEEBX_REFACTOR_BATCH_11_V1.md`

## Required verification

- Search and browse use only the isolated LifeSwitch DSN.
- Both SQL effects live in the adapter and use read-only transactions.
- Browse retains active-family, active-member, active-exercise, and public
  exercise guards.
- Route validation, response shaping, methods, paths, and OpenAPI remain exact.
- No catalog write can enter through the new reader.
- Existing and new focused tests pass in the locked runtime.
- The complete locked-runtime suite passes.

## Deployment state

Candidate only. This document and batch do not authorize deployment, restart,
database write, provider call, route retirement, authentication change,
credential change, frontend change, or cleanup. Production source, services,
databases, containers, listeners, and AWS controls remain unchanged.

## Verified results

- Twenty-three focused catalog-adapter, route-contract, isolated-PostgreSQL, and
  application-composition tests pass.
- All 1,194 locked-runtime backend tests pass.
- The changed Python files compile in the locked CPython 3.12 runtime.
- A read-only live query through the candidate adapter returns five search rows
  and 131 browse rows from isolated LifeSwitch PostgreSQL with the expected
  response fields.
- Independent read-only comparison against both database copies proves exact
  result parity: the 11-row `squat` search is SHA-256
  `9eb436afe8691eb1e790c947a02b4602800f4a84b0a6a40523ecf5987f2dd3ca`;
  the 131-row strength browse is SHA-256
  `2a1e71f8bc110024ea03f7c6eabeabb487e62e888e7c6c8086491a1db704f5dd`.
- The application remains at 143 routes and 129 OpenAPI paths. Route SHA-256
  remains `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`;
  canonical compact OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- Direct SQL/transaction callsites in `catalog/routes.py` decrease from seven
  to five; neither active exercise route retains a database-client call.
- Production Git, source, configuration, services, databases, containers,
  frontend, and AWS controls remain unchanged pending publication of this
  candidate commit.
