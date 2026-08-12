# Historical Phase 8B inactive stores package contract

> Historical, non-current record. This document describes the retired Phase 8B
> package and must not be used as current release, installation, or execution
> authority. The sole current package is under
> `ops/governed_memory/installation/current/`.

## Identity

The Phase 8B inactive stores package was rooted at
`ops/governed_memory/installation/phase8b/`. That directory and its embedded
Phase 8B schema identifiers were the protocol identities for that historical
candidate. Phase 8D later removed parallel source generations and canonicalized
the surviving Python filenames; Phase 9B subsequently retired this path.

The historical package was closed by its `package_manifest.json` and was
verified by the then-current package verifier. It contained only the fresh
isolated PostgreSQL/Qdrant stores boundary, authority and durability primitives,
the stores-only supervisor surface, migration inputs 0001/0003/0004, and a
guarded synthetic proof harness.

## Explicit exclusions

The historical package excluded migration 0002 and every source-PostgreSQL step;
application runtime and wheelhouse; HTTP/worker/pilot environments and units;
Supabase and frontend routes; provider calls; calibration and pilot operations;
backup/restore; historical imports; and production chat, account, attachment,
or structured LifeSwitch data.

It required fresh empty isolated stores. Reuse of legacy databases, Qdrant
collections, aliases, vectors, snapshots, unprocessed rows, memories, jobs,
review artifacts, preferences, or compatibility state is forbidden.

## Packaged behavior

The historical controller model had an exact 20-step stores-only plan, intent-before-effect
ordering, per-step state probes, same-attempt compensation, and content-free
receipts. The durable journal is lock-gated, hash-chained, fsynced, anchored, and
supports only the documented one-entry recovery window.

The historical proof harness constructed its exact synthetic backend internally and ran an
exact deterministic scenario matrix. It is best-effort process fencing, not a
kernel sandbox, and makes no live Linux, Docker, systemd, PostgreSQL, or Qdrant
claim.

## Truthful limitations

The historical package did not contain a complete live installation, rollback,
or activation executor. It did not consume an opaque verifier-minted package
capability or close the pre-probe/apply ownership race, terminal-seal recovery,
cross-process same-content inode replacement, or durable ledger anchoring. It
did not stage images, create secrets, install authority substrate, or produce
live disposable proof.

Its `contract.json`, `execution_contract.json`, and
`controller_runtime_contract.json` recorded the blockers at that time; they are
not current authority. Static or synthetic success was not runtime readiness.

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
