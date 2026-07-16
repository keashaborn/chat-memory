# Memory V1 V5 activation-readiness audit — 2026-07-16 UTC

Server: seebx backend. Mode: read-only, zero-write.

## Result

- `schema_installed_and_restricted`: pass
- `manual_shadow_stage`: pass
- `durable_apply`: blocked
- `shadow_retrieval`: blocked
- `prompt_influence`: blocked

All expected V5 relations exist, force RLS, and contain exactly zero live rows.
`memory_v5_writer` is `NOLOGIN`, `NOINHERIT`, and `NOBYPASSRLS`.
`brains_app` has no direct V5 table grants and has the nine controlled V5 APIs.
The 44-row predicate registry matches the installed hash and remains
`status=proposed`, `runtime_active=false`.

Durable apply is blocked because `brains_app` retains `INSERT`, `UPDATE`, and
`DELETE` on each shared V5 target: `memory.entity`, `memory.claim`, and
`memory.claim_revision`. These privileges support the legacy Memory V1 path and
must not be revoked until its callers are replaced with controlled functions.

No V5 retrieval adapter or V5 trace gate exists. The active
`memory-v1-consolidation`, `memory-v1-governance`, and `memory-v1-projection`
timers are legacy Memory V1 services and were not changed.

## Evidence

- Report:
  `/home/ubuntu/memory-v1-reviews/v5-activation-readiness-20260716T020000Z.json`
- SHA-256:
  `48274e648a2c46d237dd002e47efbf43f6bbe2ed001980fe6ae972cd029748d2`
- Mode/owner: `0600 ubuntu:ubuntu`
- Database writes: `0`
- Qdrant writes: `0`
- External model calls: `0`

Focused V5 contract tests and the full isolated `memory_v1_schema_ci` suite
passed after the audit gate was added.

## Next boundary

Build and clone-test the hash-locked, owner/evidence-scoped manual V5 staging
runner. It may call `stage_relational_packet_v5` only. It must not call entity
resolution apply, projection review/apply, Qdrant, retrieval, or prompt paths.
