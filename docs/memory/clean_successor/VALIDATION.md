# Governed Memory validation boundary

## What validation proves

The current validators read repository files and exercise a sealed synthetic
model. They can prove exact package membership, hashes, dependency closure,
closed contracts, deterministic controller behavior, refusal paths, absence of
forbidden predecessor imports, and that the current source-bound runtime was
rebuilt from hash-locked offline wheels.

The build receipt alone cannot prove PostgreSQL, Qdrant, Docker, systemd,
networking, secrets, routes, or application consumption. The separate Phase 8G
receipt proves the exact current candidate in invocation-owned disposable
resources only; it is not installation or live-production evidence.

## Canonical repository checks

Run first on **Local Mac** from the isolated Phase 9D candidate. After the exact
patch is transferred, repeat the same checks on **seebx** from its isolated
candidate worktree:

```bash
python3 -I -B tools/governed_memory_install/package.py verify-package
python3 -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
python3 -I -B tools/governed_memory_validation/verify_migration_manifest.py \
  governed-memory-migrations
python3 -I -B tools/governed_memory_release/release_guard.py verify-artifacts
python3 -B -m unittest discover -s tests/memory -p 'test_*.py'
git diff --check
```

These are offline checks. They verify the current bytes, current runtime build
receipt, promoted Phase 8G proof, and archived Phase 7C evidence. They do not run
Docker or connect to a store. The release guard remains fail-closed because
dormant installation is not authorized and later activation blockers remain.

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

- Phase 7C: runtime and application/chat-deletion receipts archived under
  `ops/governed_memory/history/phase7c/`; historical and non-reusable for the
  current candidate.
- Current runtime build: successfully rebuilt from hash-locked offline wheels;
  source-bound receipt current; not Docker, store, installation, or activation
  proof.
- Current installation package: exact manifest membership statically verified;
  claim-bound
  non-CLI install and empty-rollback compositions, durable journals, an anchored
  identity ledger, canonical operation receipts, a create-once durable receipt
  store whose public entrypoints require the canonical root-owned executions
  path, fresh terminal-readiness replay checks, opaque rollback receipt/ledger
  binding, a secure runtime verifier, an exact release-path launcher,
  resolved-spec Docker-label binding, one stopped-store empty-rollback writer
  fence, a required fresh offline/read-only emptiness recheck under that fence,
  and retained audit hashes are packaged. The verifier closes the
  complete manifest-defined release tree with no extras and requires exact
  equality between the locked and installed normalized distribution sets. The
  rollback authority claim validates trusted time and the exact global lock
  before any durable receipt read or eligibility-receipt persistence. Its
  release-tree hash is bound through the claim, journal, host ownership, and
  install receipt. Empty rollback separately requires the verified runtime
  capability and binds the exact runtime/release identity through signed
  authority, claim, journal, operation requests and observations, writer fence,
  retained install receipt, and rollback receipt.

  Phase 9D additionally packages closed post-claim Linux install and dependency
  factory code, a fixed loopback readiness DTO adapter, a physical ledger-bound
  empty-rollback adapter, and controller runtime/release builder orchestration.
  These reviewed layers expose only typed, exact operations. They do not ship a
  selected live Linux/Docker/systemd/root-file/Qdrant transport or a pinned
  PostgreSQL driver, and the builder has no live publication transport or bound
  approved standalone CPython substrate. The runtime verifier was not executed,
  no controller runtime was built, staged, or installed, no release was
  published, no installation occurred, and current live installation state was
  not reverified.
- Current full-chain migration: artifact integrity and Phase 8G disposable
  apply/rollback/absence/reapply verified; not production-applied.
- Current disposable PostgreSQL/Qdrant validation: passed against commit
  `c8691f0bef993b8e2edda982fe634c4b83e68590`, tree
  `9d95027a736a44d394c0f859821daa1554c88e22`, PostgreSQL 16.14, and Qdrant
  1.19.0. The proof log SHA-256 is
  `9ff51264aff1c56d2c8570311ba116b64e1adae8d0282bae2cd395a745a400c1`.
- Synthetic controller and in-process composition tests: repository evidence
  only; not promoted as live installation or rollback proof.
- Release: refused with `inactive_installation_package_not_authorized`.
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

The Phase 8G application-runtime rebuild used hash-locked offline wheels. The
successful disposable run used
only invocation-owned PostgreSQL/Qdrant resources with pinned local image
digests, synthetic inputs, no persistent mounts, zero provider calls, and zero
production reads or endpoint calls. Independent postflight checks found no
owned resources or listeners and confirmed unchanged live repository, service,
and Docker identities. Phase 9D added repository code and exercised only
synthetic in-process tests; it did not run Docker, access secrets, read
production data, call a provider, build or install the controller runtime, or
change a service, PostgreSQL, or Qdrant. Source preparation, installation, and
production operations remain separately authorized work.

An earlier failed attempt produced no terminal receipt. Its fail-closed worker
mode exposed stale test setup and receipt labels; cleanup completed before the
corrected canonical run. Do not combine either log or any partial receipt from
that attempt with the successful proof.

Disposable chat-erasure validation remained limited to chat-owned data and
conversational derivatives. Representative synthetic structured LifeSwitch
fixtures were hash-identical before and after, but production LifeSwitch data
was not inspected. Accounts and structured LifeSwitch libraries, food logs,
workouts, weightlifting sessions, measurements, plans, and people data remain
excluded from deletion.
