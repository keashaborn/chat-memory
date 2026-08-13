# Governed Memory clean successor

## Current repository authority

Phase 9H retains one current successor path and one canonical dormant-store
controller package. The repository-only package is the only current stores-only
package:

- package manifest: `ops/governed_memory/installation/current/package_manifest.json`;
- offline package verifier: `tools/governed_memory_install/package.py`;
- store-migration verifier:
  `tools/governed_memory_validation/verify_store_migration_manifest.py`;
- controller: `tools/governed_memory_install/controller.py`;
- authority verifier: `tools/governed_memory_install/authority.py`;
- durable journal: `tools/governed_memory_install/journal.py`;
- durable create-once receipt store:
  `tools/governed_memory_install/durable_receipts.py`;
- closed post-claim Linux effects and dependency factory:
  `tools/governed_memory_install/linux_store_effects.py`;
- fixed loopback readiness DTO adapter:
  `tools/governed_memory_install/linux_store_readiness.py`;
- ledger-bound physical empty-rollback adapter:
  `tools/governed_memory_install/rollback_live_adapter.py`;
- controller runtime/release builder orchestration:
  `tools/governed_memory_release/controller_runtime_builder.py`;
- immutable Docker, systemd, root-file, and Qdrant request/observation contracts:
  `tools/governed_memory_install/linux_live_transports.py`;
- closed non-PostgreSQL Linux adapters:
  `tools/governed_memory_install/linux_live_adapters.py`;
- durable empty-rollback controller marker:
  `tools/governed_memory_install/live_rollback_marker.py`;
- fixed PostgreSQL orchestration, resume, and catalog stage machine:
  `tools/governed_memory_install/postgres_native_stages.py`;
- runtime publication policy transport:
  `tools/governed_memory_release/runtime_publication_transport.py`;
- PostgreSQL driver-native source-closure and execution-requirements contract:
  `tools/governed_memory_install/postgres_source_closure.py` and
  `ops/governed_memory/installation/current/postgres/source_closure_contract.json`;
- synthetic proof backend: `tools/governed_memory_install/synthetic_backend.py`.

Phase 8D removed the parallel Phase 8A successor-install implementation. Phase
9B moved the surviving package to the phase-neutral `installation/current/`
identity and moved prior phase material under the non-executable history roots.
Phase 9D repaired the install and empty-rollback contracts. Phase 9F repaired
runtime-input and receipt provenance and closed the PostgreSQL source
requirements. Phase 9H adds executable non-PostgreSQL Linux transports, a fixed
PostgreSQL orchestration/resume/catalog machine, publication-receipt validation,
and a crash-recoverable rollback marker. It does not package the concrete
Psycopg SQL adapter or production runtime-publication primitives. Nothing is
installed or activated.

The current store manifest contains nine store artifacts. The current package
membership and count are authoritative only in its generated manifest. The
global `schema_contract.json` is verified by the full-chain migration manifest
and is intentionally excluded from the stores-only package.

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
harness, and claim-bound non-CLI install and empty-rollback controller
compositions with durable journals, a create-once durable receipt store, an
anchored resource ledger, and canonical operation receipts. Install receipt
creation and replay require a fresh post-migration readiness proof. Public
install and rollback entrypoints accept that receipt store only at the
canonical root-owned executions path; caller-selected synthetic stores are
restricted to private in-process test wrappers. Empty rollback requires an
opaque exact install-receipt/ledger binding and binds retained audit-artifact
hashes. Phase 9H now orders durable controller-marker acquisition, a live
semantic-empty recheck, exact store stop and removal, and final receipt
persistence before marker release. The marker is evidence of controller
authority; it is not a PostgreSQL or Qdrant lock and cannot exclude a
privileged process or direct external client. Real writer exclusion therefore
remains a live-authorization blocker. Its effects are
derived from exact physical ledger targets rather than caller-supplied
commands, endpoints, or resource names.

The package includes a closed post-claim Linux install adapter and dependency
factory, fixed loopback readiness DTO adapter, physical ledger-bound
empty-rollback adapter, secure runtime-verification capability, exact
release-path supervisor launcher source, and controller runtime/release builder
orchestration. The verifier requires the complete manifest-defined release tree
with no extra, linked, special, or writable members and requires the installed
normalized distribution set to exactly equal the hash-locked controller
dependency set. The release-tree hash is carried through the install execution
claim, durable journal, host ownership requests, and final install receipt. The
separate rollback signature binds the verified controller-runtime receipt; the
exact runtime and release-tree identities continue through rollback authority,
claim, journal, every operation request and observation, controller marker, retained
install-receipt check, and final rollback receipt.
The rollback authority, trusted time, exact global lock, and single-use nonce
are validated and claimed before any durable receipt read or eligibility-receipt
persistence.

Phase 9H ships immutable low-level Docker, systemd, root-file, and Qdrant
request/observation contracts plus concrete non-PostgreSQL Linux adapters. It retains two narrowly executable earlier
primitives: selected-field local-image inspection and exact-ledger-container-ID
supervisor inspect/start/stop. Their Docker projections were reduced to
non-secret fields and the stop flag was corrected. The complete bound transport
factory remains blocked specifically by the absent concrete Psycopg adapter.
Absence is distinct from unknown failure, systemd partial
prefixes require manual review, and root-file removal requires device, inode,
content-hash, and execution identity. The PostgreSQL
contract closes the canonical source hashes and required roles, timeouts,
advisory-lock lifecycle, privacy predicates, catalog authority, and rollback
prefix semantics. The Phase 9H machine locks before every endpoint, boundary,
prefix, and catalog observation and normalizes 16 fixed catalog queries,
including full policy semantics, trigger enabled state, columns/defaults,
indexes, schema ownership/ACLs, and default ACLs. It still refuses construction
without operation-to-SQL translation, a concrete adapter, and an independently
approved terminal catalog. Psycopg with its binary extra, version 3.3.4, is
only the preferred synchronous driver family and version. No exact wheel
filenames or hashes have been selected, locked, staged, verified, or
native-closure inspected. The Astral CPython 3.12.13
archive identity is selected, but its
bytes, members, and payload tree are not staged or verified. A canonical
wheelhouse contract exists without a staged wheelhouse or tree hash, and no
approved terminal PostgreSQL catalog exists; live execution must refuse. The
runtime builder also refuses the unstaged CPython input and current
driver-incomplete lock, and the installed-runtime capability verifier rejects
any contract whose substrate, driver, or wheelhouse readiness is incomplete.
Runtime receipt v3 provenance is packaged. Publication strictly binds canonical
receipt bytes to immutable destination identities and persists a durable
publication-intent record before the first rename. Same-device rename
preconditions are checked before that intent is created. Exact terminal state
replays with renewed fsyncs; every ambiguous post-intent prefix is fenced for
manual review, and generic cleanup is forbidden. Production
filesystem/archive primitives and independent
standalone-substrate payload-tree proof remain absent. No controller runtime was
built, staged, or installed; no release was published; and no image, secret,
service, PostgreSQL, or Qdrant state was touched. There is no live installation
proof or activation executor.

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

Four shared dormant-store installation store inputs also remain current: the canonical-cluster
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

The current disposable-proof gate is closed. The next gate must stage and
verify the selected standalone CPython substrate, lock and verify Psycopg and
the canonical wheelhouse, select the terminal PostgreSQL catalog, implement the
concrete Psycopg and production runtime-publication primitives, independently
prove the standalone payload tree, and add real exclusion for every authorized
writer path, then separately authorize the exact
controller runtime/release build and disposable Linux installation proof.
Dormant installation remains a
separate ungranted authority, so release remains refused with
`inactive_installation_package_not_authorized`. Before any installation,
resolve and re-prove the remaining blockers in
`ops/governed_memory/installation/current/contract.json` and the global
production-activation blockers in `ops/governed_memory/runtime_manifest.json`.
Source PostgreSQL preparation is governed by a separate, phase-neutral
source-preparation authorization. It is not authorized by this disposable
proof or by a stores-only install signature.

The retired Phase 8A source-role template and service-account contract have no
current replacement package. Rebuilding and independently governing the source
role bootstrap and application service-identity contract are explicit blockers,
not work inherited from the retired artifacts.
