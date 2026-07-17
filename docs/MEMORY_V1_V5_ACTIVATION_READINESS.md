# Memory V1 V5 activation readiness

Status: read-only v2 gate implemented; no V5 runtime activation.

Server boundary: seebx backend. This gate does not run on Verbal Sage, RESSE,
or Resse-Train.

## Four separate boundaries

1. `schema_installed_and_restricted`: all hash-locked V5 relations exist, force
   RLS, expose no direct table grants to `brains_app`, retain the restricted
   `memory_v5_writer`, and keep the predicate registry proposed/inactive.
2. `manual_shadow_stage`: the schema boundary passes and every tracked V5 row
   and owner count exactly matches a reviewed governed-state baseline. Any
   unreviewed row or owner drift fails closed.
3. `durable_apply`: manual staging passes and `brains_app` has no direct mutation
   privileges on shared V5 durable targets (`entity`, `claim`, and
   `claim_revision`).
4. `shadow_retrieval` and `prompt_influence`: remain false. The restricted V5
   reader, selector, and retracted-claim negative control exist; hash-locked
   candidate discovery, router-side trace capture, cross-owner evaluation, and a
   separate activation gate do not.

The checker uses the local PostgreSQL container's maintenance role for one
repeatable-read, read-only transaction. The application DSN is deliberately not
used because `brains_app` cannot inspect V5 tables. The installation manifest
defines the tracked relation set; the separately hashed governed-state baseline
defines exact allowed row and owner counts. It makes
no OpenAI, Qdrant, Redis, Supabase, or application calls. Its JSON report is
written with mode `0600`.

## Required sequence

1. Run the gate against production and preserve its checksum.
2. Build hash-locked, owner-filtered V5 candidate discovery without prompt
   exposure or durable trace writes.
3. Clone-test missing actor, cross-owner candidates, retracted claims, replay,
   and negative-control turns.
4. Add a bounded router-side shadow trace contract with zero prompt/model
   exposure.
5. Compare traces across at least two owners and negative-control turns.
6. Replace legacy direct shared-target writes with controlled functions and
   revoke their table privileges.
7. Refresh the governed-state baseline after each separately reviewed durable
   transition.
8. Only then design a separately gated prompt cutover and legacy retirement.

The existing `memory-v1-consolidation`, `memory-v1-governance`, and
`memory-v1-projection` timers remain legacy Memory V1 services. None is a V5
worker, and no V5 worker or timer may reuse those names during shadow testing.
