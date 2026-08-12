# Phase 8B inactive remediation package

Phase 8B now has a separate stores-only candidate package at
`ops/governed_memory/installation/phase8b/`. It is proof-pending, unsigned,
unstaged, uninstalled, inactive, and contains no live installation or rollback
entrypoint. The Phase 8A root-level installation package and its promoted proof
remain historical, byte-preserved inputs; they are not the current Phase 8B
package and must not be used as installation authority.

## Current scope

The package defines fresh, isolated PostgreSQL and Qdrant stores, with
PostgreSQL canonical and Qdrant derived/rebuildable. It imports no legacy rows,
vectors, snapshots, attachments, memories, jobs, review artifacts, or
compatibility state. It includes canonical migrations 0001, 0003, and 0004.
The source conversation bridge migration 0002 is excluded and remains Phase 8C.

The PostgreSQL 16 invalid-mode defect is corrected only in the Phase 8B-scoped
copy of `roles_preflight.pgsql`: it enables `ON_ERROR_STOP` and raises SQLSTATE
`22023`. The historical Phase 8A migration input is unchanged. The identical
numeric `\quit` defect in migration 0002 is not repaired here.

The package adds deterministic primitives for an exact global lock, trusted
UTC window enforcement, persistent single-use nonce claims, operation-bound
authority, a claim-bound lock-gated fsynced journal with its integrated
authority-state anchor, already-local image identity checks,
exact Docker argv planning with pull forbidden, exact resource identity
ledgering, and a stores-only supervisor. The supervisor may inspect, start, and
stop only the two ledger-bound container IDs. It cannot pull, create, recreate,
delete, or match targets by wildcard or prefix.

The static store specification uses one canonical candidate ID,
`governed_memory_9a54cf123493_000001`, and a separately named Docker resource
slug, `governed-memory-9a54cf123493-000001`. Every candidate label uses the
canonical ID. Resource names are derived exactly from the Docker slug.

Store bootstrap values are separated by container. Future execution must create
`postgres.env` with exactly `POSTGRES_DB=postgres`,
`POSTGRES_USER=governed_memory_bootstrap`, and a fresh `POSTGRES_PASSWORD`; it
must not contain the Qdrant key. It must create `qdrant.env` with only a fresh
`QDRANT__SERVICE__API_KEY`; it must not contain any PostgreSQL key. Both files
are separate root-owned `0600` targets. The initial PostgreSQL database remains
`postgres` because plan step I12 creates `governed_memory` after the fresh-store
preflight.

The pure Docker plan now binds `restart=no`, `no-new-privileges`, capability
drop/add policy, a 256-process limit, bounded `json-file` logs, an isolated
`/tmp`, and disabled image healthchecks. A correct external PostgreSQL/Qdrant
readiness probe is intentionally not implemented until the pinned images can be
proved in disposable Linux without staging them under this approval.

The identity ledger seals container IDs, image IDs/digests, labels, and named
resource events. The supervisor checks container ID, name, image, labels,
command, required and cross-store-forbidden environment keys, mount, network, loopback
port, restart, process limit, capabilities, security options, logging, and the
disabled container-healthcheck setting before start or stop. A closed allowlist
for pinned-image baseline environment keys, external readiness, database, role, migration,
collection, alias, and unit postconditions still require exact live probes.

The 20-step future plan is a contract only. The hermetic controller tests
ordering, drift refusal, the durable journal/anchor adapter, one-entry
file-ahead reconciliation, and the same-attempt compensation algorithm before
the terminal seal. This does not prove end-to-end process or composite-step
crash recovery. The controller has no installation host adapter and executes no
host commands. A later successful-install
rollback requires its own signed operation and cannot reuse install authority.

## Explicit exclusions

This phase does not read, stat, hash, rename, delete, or reuse
`/etc/governed-memory/pilot.env`. It contains no application service account,
application runtime, HTTP/worker/pilot environment, application unit, source
PostgreSQL role or bridge change, Supabase or frontend route, provider call,
calibration, pilot, backup/restore, legacy quiescence, or production chat,
account, attachment, or structured LifeSwitch data operation.

## Verification boundary

Offline verification entrypoints are:

```text
python3 -I -B tools/governed_memory_install/package_v3.py verify-package
python3 -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
python3 -B tools/governed_memory_validation/generate_phase8b_package_manifest.py
python3 -I -B tools/governed_memory_validation/run_phase8b_disposable_proof.py
```

The generator prints the deterministic package manifest for review; it never
writes it. After changing any member, regenerate externally, compare, and
replace the checked-in manifest in a separately reviewed patch.

Passing static tests does not prove PostgreSQL execution, Linux installation,
systemd behavior, Docker resource creation, crash recovery on a real host, cold
restart, or empty rollback. No Docker image may be staged and no authority,
secret, unit, database, collection, or service may be installed under the
current Phase 8B package approval.

Before any dormant installation can be considered, complete a separate
proof-metadata promotion for this candidate, add a separately signed rollback
verifier, establish the root-owned trust and image-staging substrate under
separate authority, stage and receipt exact image IDs, bind a current live
read-only preflight, prove the package on disposable Linux with PostgreSQL 16
and Qdrant, and obtain a new signed dormant-install approval bound to the final
commit, tree, package, contract, plan, scope, and image identities.

## Inactive execution and synthetic-proof extension

The next repository-only extension is governed by
`execution_contract.json` and `controller_runtime_contract.json`. It packages
an opaque claim-derived execution binding, a Phase 8B-specific durable journal,
and a guarded synthetic proof harness. Packaging
those components does not authorize calling them against a host. The harness
uses only synthetic state and cannot prove Docker, systemd, PostgreSQL, Qdrant,
external readiness, cold restart, or Linux crash recovery.

The claim-derived binding currently rehashes the signed scope and exact raw
manifest, contract, plan, store-spec, and resource-identity implementation
inputs. It does not require a capability minted by the closed package verifier.
An opaque verified-package capability and the boundary that consumes it remain
a blocker before any complete live executor may be packaged.

The controller runtime is intentionally separate from the successor
application runtime. It has its own hash-locked dependency input but has not
been built, receipted, installed, or selected by systemd. Repository source and
the system Python module path are not execution authority.

Crash-safety claims remain narrow. The package may prove journal/anchor
reconciliation and recovery after a compensation effect precedes its journal
receipt. It does not yet recover the terminal seal or the partial states of
composite operations such as two-file secret creation, PostgreSQL bootstrap,
unit install-and-enable, or cold restart plus postflight. Those operations must
be decomposed or supplied with exact durable subjournals and then proved on
disposable Linux before a live installation executor can exist.

The journal detects file or directory replacement while the same journal
instance remains open. Across a process reopen it verifies canonical bytes,
the hash chain, and the durable anchor, but the anchor does not persist a file
generation identity. Replacement with identical bytes and safe metadata is
therefore not detected across processes. A portable durable file-generation
seal or an equivalent design remains required before live execution.

An adversarial review rejected and removed the initial typed host-adapter
draft because it exposed unsafe callback and inspection surfaces. The current
package retains the earlier generic bounded Docker runner, local-image inspect
adapter, and stores supervisor's narrow inspect/start/stop surface for
preexisting exact ledger-bound container IDs. Those primitives are not bound to
the new claimed execution capability. There is no claim-bound installation
runner composition, secret generator or publisher, readiness adapter, store
effect backend, live installation entrypoint, or rollback executor. The
supervisor cannot install, pull, create, recreate, or delete resources. Missing
execution components must be redesigned against the final claim-bound journal
before inclusion.
In particular, a future typed apply boundary must atomically return and verify
attempt-bound ownership identity. The hermetic `BEFORE` re-probe cannot close a
real-host race in which another actor creates a target between probe and apply;
no live ownership or safe-compensation claim is made here.

The proof fence is Python audit instrumentation, not kernel confinement. It is
intended to detect ordinary accidental process, network, environment, and
out-of-bound filesystem effects during the synthetic run. It does not resist
hostile code in the same interpreter. The synthetic backend seal and opaque
capabilities are API boundaries within a trusted exact controller process, not
security boundaries against hostile same-process mutation.
The no-argument runner creates and removes its own private temporary root. The
path-taking harness function is a test helper: it accepts a caller-owned empty
`0700` disposable root and leaves cleanup to that caller. Synthetic unit
execution needs no signed host authority, produces no promoted receipt, and is
not the separately authorized live disposable-Linux proof.

The prior Phase 8B manifest at commit
`d7758ef4af9397728604709661a0b4f8f1f0daf4` is historical package evidence,
not execution authority for this extension. Git retains its exact bytes; the
current package must have one closed manifest and one verifier when finalized.
