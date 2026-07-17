# Memory V1 V5 activation readiness

Status: read-only gate implemented; no V5 runtime activation.

Server boundary: seebx backend. This gate does not run on Verbal Sage, RESSE,
or Resse-Train.

## Four separate boundaries

1. `schema_installed_and_restricted`: all hash-locked V5 relations exist, force
   RLS, expose no direct table grants to `brains_app`, retain the restricted
   `memory_v5_writer`, and keep the predicate registry proposed/inactive.
2. `manual_shadow_stage`: the schema boundary passes and every V5 owner/staging
   relation is empty before the first controlled packet.
3. `durable_apply`: manual staging passes and `brains_app` has no direct mutation
   privileges on shared V5 durable targets (`entity`, `claim`, and
   `claim_revision`).
4. `shadow_retrieval` and `prompt_influence`: always fail closed until a V5-only
   retrieval adapter, trace contract, cross-owner audit, and separate activation
   gate exist.

The checker uses the local PostgreSQL container's maintenance role for one
repeatable-read, read-only transaction. The application DSN is deliberately not
used because `brains_app` cannot inspect V5 tables. It makes
no OpenAI, Qdrant, Redis, Supabase, or application calls. Its JSON report is
written with mode `0600`.

## Required sequence

1. Run the gate against production and preserve its checksum.
2. Build a hash-locked, owner/evidence-scoped manual staging runner.
3. Clone-test missing actor, cross-owner, altered packet, replay, and rollback.
4. Stage one reviewed owner batch; do not apply durable projections.
5. Audit the staged entity, temporal, observation, and deferral rows.
6. Replace legacy direct shared-target writes with controlled functions and
   revoke their table privileges.
7. Apply a reviewed batch transactionally and prove replay writes zero rows.
8. Add read-only V5 shadow retrieval with zero prompt/model exposure.
9. Compare traces across at least two owners and negative-control turns.
10. Only then design a separately gated prompt cutover and legacy retirement.

The existing `memory-v1-consolidation`, `memory-v1-governance`, and
`memory-v1-projection` timers remain legacy Memory V1 services. None is a V5
worker, and no V5 worker or timer may reuse those names during shadow testing.
