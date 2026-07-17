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
   reader, selector, retracted-claim negative control, hash-locked candidate
   discovery, and router-side zero-write trace pass isolated/clone tests. They
   are not deployed or live-audited; a separate activation gate does not exist.

The checker uses the local PostgreSQL container's maintenance role for one
repeatable-read, read-only transaction. The application DSN is deliberately not
used because `brains_app` cannot inspect V5 tables. The installation manifest
defines the tracked relation set; the separately hashed governed-state baseline
defines exact allowed row and owner counts. It makes
no OpenAI, Qdrant, Redis, Supabase, or application calls. Its JSON report is
written with mode `0600`.

## Required sequence

1. Run the gate against production and preserve its checksum.
2. Deploy the disabled-by-default V5 shadow trace code without changing service
   configuration.
3. Re-run the retracted-claim negative control and governed-state readiness
   gate against the deployed code.
4. Enable zero-write shadow tracing for explicit test owners only.
5. Compare live traces across at least two owners and negative-control turns.
6. Replace legacy direct shared-target writes with controlled functions and
   revoke their table privileges.
7. Refresh the governed-state baseline after each separately reviewed durable
   transition.
8. Only then design a separately gated prompt cutover and legacy retirement.

The existing `memory-v1-consolidation`, `memory-v1-governance`, and
`memory-v1-projection` timers remain legacy Memory V1 services. None is a V5
worker, and no V5 worker or timer may reuse those names during shadow testing.
