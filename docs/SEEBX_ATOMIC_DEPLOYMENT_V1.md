# SeeBx atomic deployment gate v1

This gate separates building a runtime from activating production. The builder
creates an immutable, versioned virtual environment and a content-free receipt.
It cannot change `/opt/chat-memory/venv`, Git refs, PostgreSQL, systemd, Zep, or
the paired Verbal Sage frontend.

## Verified release boundary

- SeeBx candidate base: `299d231f5970659e5aa5e197ac1756ad4483efdc`, a
  clean 142-commit fast-forward from production
  `49f9e60cf4321c8e42c359845c1a62a8c987614d`.
- Paired Verbal Sage candidate:
  `4a9ae6dce79849bfbd4d330e0580a03b603a529c`, a clean five-commit
  fast-forward from production `858b61527186571cabb5580dc5159fdf6b69ae2e`.
- The disposable database migration gate passed before this runtime gate was
  designed. It never called Zep or changed the live `memory` database.
- The Linux x86_64 CPython 3.12 wheel set contains 43 exact wheels. A fresh
  offline installation passed `pip check`, the 12-of-12 dependency preflight,
  all twelve provider/framework imports, and all 1,087 backend tests.

## Build boundary

`scripts/build_runtime_environment.py` must run as root with:

- a clean candidate Git worktree that is a strict fast-forward or exact match
  of the named production commit;
- the committed `requirements-runtime.lock`;
- the committed wheelhouse manifest; and
- a private root-owned wheelhouse outside the repository; and
- a root-owned `0755` output root that the non-root service can traverse but
  cannot modify.

The builder verifies every wheel filename, byte count, and SHA-256 digest. It
installs with `--isolated --no-index --only-binary=:all: --require-hashes`,
runs `pip check`, enforces the exact 12-package project contract, imports every
declared runtime module, makes the environment read-only, atomically publishes
it, and records `activated=false` in the receipt.

## Production activation boundary

Activation remains a separate, explicit production authorization. The later
controller must fail closed unless all of these are bound in one release
receipt:

1. exact backend and frontend commits and trees;
2. the immutable runtime-environment receipt;
3. the successful disposable migration receipt and verified recovery backup;
4. captured systemd, environment-file, Git, database, service, timer, and
   frontend build rollback evidence;
5. exact pre-cutover service and health baselines;
6. a quiescence plan for backend/frontend writes;
7. forward database migrations, backend fast-forward, runtime swap, systemd
   preflight installation, and paired frontend activation in one bounded
   window; and
8. reverse-order rollback, including an empty Zep outbox requirement before
   database rollback.

No component is considered deployed because this build gate passes.
## Offline paired release-package gate

`ops/releases/seebx_atomic_release_package_v1.schema.json` and
`scripts/verify_atomic_release_package.py` define the content-addressed handoff
between preparation and a later production controller. The package must bind:

- clean, remote-matched backend and frontend candidate commits and trees;
- independently verified full Git bundles for both source histories, a
  source/tree-bound frontend build manifest and archive, and the runtime
  receipt;
- the successful disposable migration package and receipt, the recovery
  receipt, and the exact database backup;
- systemd snapshots and content-free environment manifests from both servers;
- service/timer baselines, a reverse-order rollback, and an empty Zep outbox
  before any database rollback; and
- an explicit service-stop quiescence plan.

Every artifact is a unique canonical relative path with an exact SHA-256 hash.
The verifier reconstructs both Git bundles in disposable bare repositories and
proves the production-to-candidate ancestry, tree hashes, branch heads, and
ahead counts. It opens the frontend archive without extracting it, rejects
traversal, duplicate paths, links, devices, pipes, and every `.env` path, and
requires the standalone server entrypoint plus public, static, and rendered
route artifacts. It cross-checks those observed counts and bytes against the
frontend build manifest and binds the manifest and runtime receipt to their
source commits/trees, then cross-checks the migration and recovery receipts
against the same database backup. The package cannot authorize itself:
its schema requires
`production_activation_authorized=false`, `authorization_id=null`, and
`approved=false` for quiescence. A passing result means package integrity is
ready for a separate authorization decision; it does not mean deployment is
ready or approved.
