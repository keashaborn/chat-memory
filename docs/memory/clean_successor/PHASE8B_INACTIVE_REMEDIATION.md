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
authority, an unintegrated hash-only journal-anchor state primitive,
already-local image identity checks,
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
ordering, drift refusal, and the same-attempt compensation algorithm before the
terminal seal. It has no durable journal/anchor adapter and does not prove
crash recovery. It does not execute host commands. A later successful-install
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
