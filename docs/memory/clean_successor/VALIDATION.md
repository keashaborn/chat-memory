# Governed Memory validation boundary

## What validation proves

The current validators read repository files and exercise a sealed synthetic
model. They can prove exact package membership, hashes, dependency closure,
closed contracts, deterministic controller behavior, refusal paths, absence of
forbidden predecessor imports, and that the current source-bound runtime was
rebuilt from hash-locked offline wheels.

The current build receipt cannot prove PostgreSQL, Qdrant, Docker, systemd,
networking, secrets, routes, or application consumption. Source code, tests,
manifests, build receipts, and synthetic receipts are not disposable or live
runtime evidence.

## Canonical repository checks

Run on **seebx** from the isolated candidate worktree:

```bash
python3 -I -B tools/governed_memory_install/package.py verify-package
python3 -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
python3 -I -B tools/governed_memory_validation/verify_migration_manifest.py \
  governed-memory-migrations
python3 -I -B tools/governed_memory_release/release_guard.py verify-artifacts
python3 -B -m unittest discover -s tests/memory -p 'test_*.py'
git diff --check
```

These are offline checks. They verify the current bytes and current runtime
build receipt. They require the release guard to remain fail-closed because the
current disposable PostgreSQL/Qdrant proof does not exist. They do not run
Docker or connect to a store.

## Closed current package

The checked-in manifest must be byte-identical to generator output. Every local
Python import and package initializer must be a manifest member. Extra files,
old version-suffixed module names, Phase 8A source, migration 0002, runtime
credentials, application services, and legacy proof payloads are forbidden from
the stores-only package.

The current canonical modules are unversioned filenames. Schema-version values
inside signed or hashed documents remain explicit protocol identities and are
not rewritten merely to make filenames shorter.

## Evidence classifications

- Phase 7C: application/chat-deletion disposable evidence and runtime build
  receipt retained as archived historical evidence; inactive and non-reusable
  for the current candidate.
- Current runtime build: successfully rebuilt from hash-locked offline wheels;
  source-bound receipt current; not Docker, store, installation, or activation
  proof.
- Current installation package: statically verified and synthetic-proof capable;
  no installation occurred, and current live installation state was not
  reverified.
- Current full-chain migration: artifact integrity verified; disposable
  revalidation required because migration 0002 and the active runtime changed.
- Current disposable PostgreSQL/Qdrant validation: pending; no Docker proof has
  run yet.
- Synthetic controller receipt: unit/model evidence only; not promoted as live
  installation proof.
- Release: refused with `current_candidate_disposable_proof_missing`.
- Production: no successor installation or activation evidence; no production
  data read and no provider call made.

Any future live claim must separately bind source, installed bytes,
configuration, enablement, running process, invocation, and consumption.

## Retirement verification

Phase 8D requires all of the following:

1. no executable Phase 8A successor-install source or test remains in the active
   tree;
2. no current successor control, CI job, test, or document treats Phase 8A as
   authority;
3. current exact inventories name only canonical successor modules;
4. the release guard verifies the current package and store migration manifest;
5. deleted artifact hashes are retained only in the compact retirement ledger;
6. Git history, rather than an active-tree code archive, preserves old bytes;
7. focused and full tests pass without Docker or live-store access.

Phase 8F additionally requires that the current app/response graph and default
CI have no Memory V1 or stored-preference dependency. Remaining V1/v5 files are
quarantined and are not considered current proof. Their physical deletion is a
later, separately authorized closure.

## Safety boundary

The completed rebuild used only hash-locked offline wheels. It installed or
activated nothing, touched no secret, made no provider call, read no production
data, and changed no live service, PostgreSQL database, or Qdrant collection.
Phase 8G may next use only invocation-owned disposable PostgreSQL/Qdrant
resources; no such Docker proof exists yet. Source preparation and production
operations remain separately authorized work.

Disposable chat-erasure validation must remain limited to chat-owned data and
conversational derivatives. Accounts and structured LifeSwitch libraries, food
logs, workouts, weightlifting sessions, measurements, plans, and people data
remain excluded.
