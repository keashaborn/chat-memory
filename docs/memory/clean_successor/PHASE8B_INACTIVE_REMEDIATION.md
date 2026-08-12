# Inactive stores package contract

## Identity

The canonical inactive stores package is rooted at
`ops/governed_memory/installation/phase8b/`. The directory and embedded Phase 8B
schema identifiers are stable protocol identities. Phase 8D removed parallel
source generations and canonicalized the surviving Python filenames.

The package is closed by `package_manifest.json` and verified by
`tools/governed_memory_install/package.py`. It contains only the fresh isolated
PostgreSQL/Qdrant stores boundary, authority and durability primitives, the
stores-only supervisor surface, migration inputs 0001/0003/0004, and a guarded
synthetic proof harness.

## Explicit exclusions

The package excludes migration 0002 and every source-PostgreSQL step;
application runtime and wheelhouse; HTTP/worker/pilot environments and units;
Supabase and frontend routes; provider calls; calibration and pilot operations;
backup/restore; historical imports; and production chat, account, attachment,
or structured LifeSwitch data.

It requires fresh empty isolated stores. Reuse of legacy databases, Qdrant
collections, aliases, vectors, snapshots, unprocessed rows, memories, jobs,
review artifacts, preferences, or compatibility state is forbidden.

## Packaged behavior

The controller model has an exact 20-step stores-only plan, intent-before-effect
ordering, per-step state probes, same-attempt compensation, and content-free
receipts. The durable journal is lock-gated, hash-chained, fsynced, anchored, and
supports only the documented one-entry recovery window.

The proof harness constructs its exact synthetic backend internally and runs an
exact deterministic scenario matrix. It is best-effort process fencing, not a
kernel sandbox, and makes no live Linux, Docker, systemd, PostgreSQL, or Qdrant
claim.

## Truthful limitations

The package does not contain a complete live installation, rollback, or
activation executor. It does not yet consume an opaque verifier-minted package
capability. It does not close the pre-probe/apply ownership race, terminal-seal
recovery, cross-process same-content inode replacement, or durable ledger
anchoring. It has not staged images, created secrets, installed authority
substrate, or produced live disposable proof.

`contract.json`, `execution_contract.json`, and
`controller_runtime_contract.json` are authoritative for the remaining exact
blockers. Static or synthetic success must not be described as runtime
readiness.

## Historical separation

The prior root-level Phase 8A successor-install package and executable stack were
retired in Phase 8D. Their exact identities are recorded in the compact
retirement ledger and in Git history; their source and proof payloads are not
duplicated in the active tree. Retained application/migration artifacts and the
separate Memory v1/v5 repository runtime outside this stores-only package remain
separately governed and must not be inferred to be obsolete merely because they
are not package members.

Source logging and the inactive conversation bridge require separate,
phase-neutral source-preparation authorization. Repository cleanup grants no
such authority.
