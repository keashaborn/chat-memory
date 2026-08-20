# SeeBx refactor batch 08: prior-web provenance repository boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves prior trusted-web provenance transaction
authority and SQL out of the conversation capability. The capability retains
the narrow user-intent gate, snapshot validation, row-level cryptographic and
ownership validation, source sanitization, deterministic selection, byte cap,
and immutable provenance envelope. The new
`seebx.adapters.prior_web_provenance_postgres` adapter owns the restricted
repeatable-read transaction, actor setting, database-role and read-only checks,
owner-thread existence gate, transcript join, cutoff, ordering, and limit.

The public loader signature and all composition callers remain unchanged.
Repository errors remain content-free and preserve the existing capability
error messages.

## Changed files

- `seebx/capabilities/conversation/prior_web_provenance.py`
- `seebx/adapters/prior_web_provenance_postgres.py`
- `tests/test_prior_web_provenance_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_08_V1.md`

## Verified invariants

- Unrelated questions perform zero database work.
- Reads remain repeatable-read, read-only, `brains_app`, actor-bound, and
  owner-thread-bound.
- The exact chat-log/transcript join, source filter, cutoff tuple, ordering,
  and candidate cap are preserved.
- Cross-owner, hash-tampered, non-admitted, non-public, and non-HTTPS sources
  remain excluded; source titles, excerpts, queries, fragments, and credentials
  remain absent from lower-authority prompt content.
- The capability contains no SQL or direct database calls; direct-database
  capability files decrease from 15 to 14.
- All 9 focused tests and all 1,183 locked-dependency runtime tests pass.
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

This batch does not correct the separate prior-LifeSwitch layering defect; that
path remains the next bounded provenance extraction after this contract is
verified.
