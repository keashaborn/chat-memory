# SeeBx refactor batch 09: prior-LifeSwitch provenance query boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves the remaining prior-LifeSwitch provenance SQL
out of the conversation capability and into the existing restricted PostgreSQL
adapter. The capability retains the narrow prior-source trigger, snapshot
binding, row-level owner/thread/cutoff validation, attestation, binding and
receipt verification, historical fallback, deterministic selection, content
budget, and immutable lower-authority provenance envelope.

The existing provider remains responsible for proving the user intent before
opening a database session. The existing restricted session retains the
owner-bound gateway context, read-only repeatable-read transaction, dedicated
reader role, exact query ordering and limit, and mandatory context cleanup.

## Changed files

- `seebx/capabilities/conversation/prior_lifeswitch_provenance.py`
- `seebx/adapters/lifeswitch_prior_provenance_postgres.py`
- `tests/test_prior_lifeswitch_provenance_v1.py`
- `tests/test_lifeswitch_prior_answer_provenance_runtime_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_09_V1.md`

## Verified invariants

- Unrelated and unbound questions perform zero provenance-query work.
- The capability contains no SQL or direct database calls; direct-database
  capability files decrease from 14 to 13.
- The exact owner-read context, GUCs, reader role, transaction mode, query,
  arguments, cutoff tuple, ordering, and candidate cap are preserved.
- Cross-owner, cross-thread, cutoff-invalid, unattested, cross-linked,
  hash-tampered, and malformed rows remain excluded.
- Prompt provenance remains content-free and bounded.
- All 12 direct focused tests, 16 surrounding integration tests, and all 1,185
  locked-dependency runtime tests pass.
- The candidate still exposes 143 routes and 129 OpenAPI paths.
- Candidate route SHA-256 remains
  `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`.
- Candidate OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- Application import succeeds without an OpenAI key under synthetic,
  non-connectable database configuration; the optional client remains absent.

## Deployment state

Candidate only. Production source, services, databases, containers, systemd,
AWS controls, credentials, listeners, and frontend are unchanged.
