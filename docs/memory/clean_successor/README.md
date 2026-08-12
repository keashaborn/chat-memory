# Governed Memory clean successor

## Current repository authority

There is one current Phase 8B successor installation-controller source
generation:

- package manifest: `ops/governed_memory/installation/phase8b/package_manifest.json`;
- offline package verifier: `tools/governed_memory_install/package.py`;
- store-migration verifier:
  `tools/governed_memory_validation/verify_store_migration_manifest.py`;
- controller: `tools/governed_memory_install/controller.py`;
- authority verifier: `tools/governed_memory_install/authority.py`;
- durable journal: `tools/governed_memory_install/journal.py`;
- synthetic proof backend: `tools/governed_memory_install/synthetic_backend.py`.

The `phase8b` directory and schema-version strings are retained protocol
identities. They do not identify a second current Phase 8B successor source
generation. Phase 8D removed the parallel Phase 8A successor-install
implementation and canonicalized the surviving module names.

## Current state

Phase 8D performed no installation or activation, and the repository candidate
remains activation-blocked. The repository packages an offline verifier, an
in-process synthetic proof harness, and stores-only controller primitives. It
does not package a complete live installation executor, rollback executor, or
activation executor.

Phase 8D created no persistent successor PostgreSQL or Qdrant store. It staged
or pulled no image, read or generated no secret, installed or changed no
service, and activated no application route. This repository-only phase did not
re-establish current live absence of previously created resources. A synthetic
receipt is not live proof and is not installation authority.

Phase 7C application and chat-deletion evidence remains separate historical
evidence. It does not make the installation package live.

## Retired repository material

Phase 8D removed the old 59-member Phase 8A successor-installation package, its
executable controller/verifier stack, its two obsolete test suites, and
duplicate proof payloads from the active tree. It did not retire the separate
Memory v1/v5 runtime implementation. Git history preserves the exact retired
bytes. The content-free retirement ledger is
`ops/governed_memory/history/phase8d/phase8a_successor_installation_stack_retirement.json`.

No archive copy of the deleted implementation is kept in the working tree.

## Retained boundaries

The canonical PostgreSQL migration ledger, migration 0002 conversation bridge,
Phase 7C runtime evidence, runtime/build locks, calibration contract, Supabase
session-authority contract, application HTTP/worker templates, and the existing
application-runtime validator remain. They are not part of the retired
installation-controller generation and require separate audits before any later
retirement.

Four shared Phase 8B store inputs also remain current: the canonical-cluster
forward and rollback SQL templates, `installation/store_spec.json`, and the
stores-only systemd template.

The active-tree Memory v1/v5 executable, unit, and test surface also remains. It
was not audited or retired by Phase 8D. Repository cleanup of that surface and
live host quiescence are separate future scopes.

PostgreSQL remains the canonical governed-memory authority. Qdrant is a derived,
rebuildable projection. A future installation must use fresh empty isolated
stores and must not import old memory rows, vectors, snapshots, jobs, review
artifacts, preferences, or compatibility state.

Chat deletion and memory erasure remain limited to chat-owned data. Accounts,
document capability, and structured LifeSwitch data such as food logs,
libraries, workouts, weightlifting sessions, measurements, plans, and people
data remain outside that deletion boundary.

## Legacy runtime status

Repository retirement is not runtime quiescence. A bounded read-only observation
at `2026-08-12T10:27:55Z` found the ten named legacy memory timers still loaded,
enabled, and active; their triggered services were inactive with zero main PID
at that instant. `brains.service` remained active. The previously recorded
`eval_all_users.sh` cron entry was absent. Phase 8D changed none of that state.

Stopping or deleting those installed legacy runtime surfaces requires a separate
authorization, exact pre-state capture, an observation window, and rollback
evidence. The retirement ledger preserves the exact timer/service pairs,
historical cron-line hash, 24-hour minimum observation, zero-reader/writer
requirements, unreachable provider/router/admin requirements, and the rule that
timer or cron quiescence alone does not prove exclusivity.

## Next gate

Before dormant installation, the remaining blockers in
`ops/governed_memory/installation/phase8b/contract.json` must be resolved and
re-proved. Source PostgreSQL preparation is governed by a separate, phase-neutral
source-preparation authorization. It is not authorized by repository cleanup or
by a future stores-only install signature.

The retired Phase 8A source-role template and service-account contract have no
current replacement package. Rebuilding and independently governing the source
role bootstrap and application service-identity contract are explicit blockers,
not work inherited from the retired artifacts.
