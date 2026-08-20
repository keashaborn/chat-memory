# SeeBx refactor batch 06: account-timezone repository boundary

Date: 2026-08-20

## Scope

This candidate-only batch removes PostgreSQL execution from the LifeSwitch
account-timezone HTTP capability. The capability retains authenticated actor
derivation, IANA timezone validation, request binding, response models, and
HTTP error mapping. The new
`seebx.adapters.lifeswitch_timezone_postgres` adapter owns the request-scoped
connection lifecycle, transaction modes, owner session settings, restricted
writer role, SQL gateways, row mapping, and content-free PostgreSQL error
translation.

The existing database functions, argument order, expected-revision behavior,
request hash, restricted role, owner settings, read-only transaction, response
contract, routes, and status-code mappings are unchanged.

## Changed files

- `seebx/capabilities/preferences/timezone.py`
- `seebx/adapters/lifeswitch_timezone_postgres.py`
- `tests/test_lifeswitch_account_timezone_v1.py`
- `tests/test_lifeswitch_account_timezone_repository_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_06_V1.md`

## Verified invariants

- The timezone capability no longer imports `asyncpg`, contains SQL, switches
  database roles, or directly fetches rows.
- The adapter uses the existing isolated LifeSwitch connection path and closes
  every acquired connection.
- Reads remain owner-bound and execute in a read-only transaction.
- Writes preserve the exact owner, timezone, expected-revision, and request-hash
  argument order.
- SQL states `40001` and `23505` remain revision conflicts; `22023` remains
  invalid input; `42501` remains owner-scope denial; other PostgreSQL errors
  remain service-unavailable failures.
- Focused capability, repository, and provider-boundary tests pass 11/11.
- The complete sealed-runtime suite passes 1,181/1,181.
- Parent and candidate both expose 143 routes and 129 OpenAPI paths.
- Parent and candidate route SHA-256 is
  `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`.
- Parent and candidate OpenAPI SHA-256 is
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- Application import succeeds without an OpenAI key under synthetic,
  non-connectable database configuration; the optional client remains absent.

## Deployment state

Candidate only. Production source, services, databases, containers, systemd,
AWS controls, credentials, listeners, and frontend are unchanged.
