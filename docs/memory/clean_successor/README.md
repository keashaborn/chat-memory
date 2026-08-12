# Governed Memory clean successor

## Current repository authority

Phase 8G validates one current successor path. The retained Phase 8B-named
installation-controller package is the only current stores-only package:

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

The current store manifest contains nine store artifacts. The current package
contains 43 artifacts. The global `schema_contract.json` is verified by the
full-chain migration manifest and is intentionally excluded from the
stores-only package.

## Current state

Phase 8G rebuilt the current source-bound application runtime from hash-locked
offline wheels and then passed the current-candidate disposable application,
PostgreSQL, Qdrant, and chat-deletion proof. The current build evidence is
`ops/governed_memory/runtime_build_receipt.json`; it binds the current source
tree `b52b753dc7974ee120e4abe264bb36f16b53341fa86b2ea8e0ab5bf86c735c20`
and project wheel. The proof evidence is
`ops/governed_memory/phase8g_disposable_proof_receipt.json`; it binds tested
commit `c8691f0bef993b8e2edda982fe634c4b83e68590`, tree
`9d95027a736a44d394c0f859821daa1554c88e22`, and the pre-promotion migration
manifest. The repository candidate remains inactive and activation-blocked.

The repository packages an offline verifier, an in-process synthetic proof
harness, and stores-only controller primitives. It does not package a complete
live installation executor, rollback executor, or activation executor.

The active chat response path is successor-only. It no longer accepts the old
Memory V1 prompt object, stored assistant-response preferences, or a fallback
legacy memory provider. Neutral chat-integrity attestation is separate from
governed-memory claims. The canonical chat-erasure path is limited to chat
records and their conversational derivatives.

The successful run used pinned PostgreSQL 16.14 and Qdrant 1.19.0 images,
fresh invocation-owned resources, synthetic data, and no persistent mounts. It
produced exactly one HTTP, deletion, resilience, and terminal v7 receipt.
Provider calls, production reads, and production endpoint calls were zero.
Representative synthetic LifeSwitch accounts, libraries, workouts,
weightlifting sessions, food logs, and measurements were unchanged. This does
not claim that production LifeSwitch data was read or dynamically compared.
All disposable resources were removed and the four validation ports were free
afterward. Live `brains.service`, `/opt/chat-memory`, and the production Docker
inventory retained their pre-run identities.

The first run failed closed because a test subprocess omitted the required
exclusive-successor mode; that left a synthetic row which exposed stale test
ordering and Phase 6E receipt labels. It produced no promotable receipt and its
owned resources were removed. The corrected second run is the only canonical
Phase 8G proof. No service was installed, no route activated, no secret read or
changed, and no provider call made.

The Phase 7C runtime and application receipts are archived under
`ops/governed_memory/history/phase7c/`. They remain historical evidence and do
not attest the current candidate. The exact Phase 8G proof runner is now sealed
as pre-promotion evidence; it is not rebound or presented as rerunnable against
the promoted metadata.

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

The remaining Memory v1/v5 implementation is quarantined legacy material: it is
not mounted by `app.py`, not accepted by the active response graph, and not part
of the default current CI suite. Exact unreachable-closure proof, a retained
rollback interval, and separate deletion authorization are still required
before that quarantine is physically deleted.

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

The current disposable-proof gate is closed. Dormant installation remains a
separate ungranted authority, so release remains refused with
`inactive_installation_package_not_authorized`. Before any installation,
resolve and re-prove the remaining blockers in
`ops/governed_memory/installation/phase8b/contract.json`. Source PostgreSQL preparation is
governed by a separate, phase-neutral source-preparation authorization. It is
not authorized by this disposable proof or by a stores-only install signature.

The retired Phase 8A source-role template and service-account contract have no
current replacement package. Rebuilding and independently governing the source
role bootstrap and application service-identity contract are explicit blockers,
not work inherited from the retired artifacts.
