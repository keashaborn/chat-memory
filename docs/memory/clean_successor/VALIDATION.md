# Governed Memory validation boundary

## Offline repository checks

Run on **seebx** from the isolated candidate:

```bash
python3 -I -B tools/governed_memory_install/package.py verify-package
python3 -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
python3 -I -B tools/governed_memory_validation/verify_migration_manifest.py governed-memory-migrations
python3 -I -B tools/governed_memory_release/release_guard.py verify-artifacts
PYTHONDONTWRITEBYTECODE=1 python3 -I -B -m unittest discover -s tests/memory -p 'test_*.py'
git diff --check
```

The package verifier must report schema v6, Phase 9J inactive state, exactly 74
artifacts, no proof receipt inside the sealed package, no installation, no
secret access, no image staging, and no activation. The release guard must
still refuse production release with
`inactive_installation_package_not_authorized`.

These checks prove current bytes, contracts, source closure, deterministic
controller behavior, and fail-closed paths. They do not prove Docker,
PostgreSQL, Qdrant, systemd, process-crash recovery, or installation.

## Disposable Linux proof

The external Phase 9J proof is a separate, explicitly authorized operation on
**seebx**. Only the repository issuer is invoked. It verifies the clean
candidate and exact controller-runtime receipt, creates the fixed root-owned
recovery capsule, acquires the whole-proof lock, and launches the sealed
runtime by its immutable release identity.

The proof may use only pinned local PostgreSQL 16.14 and Qdrant 1.19.0 images,
fresh invocation-owned credentials, fixed loopback ports, synthetic data, and
fixed disposable resource names. It must perform install, real controller
process termination and resume, cold controller restart, reserved empty
rollback, rollback termination and resume, and exact final absence.

The proof must not read production data or credentials, call a provider,
connect to production endpoints, alter live services, import legacy memory, or
activate the successor. Its content-free receipt is external to the sealed
package and is promotable only when all terminal checks pass.

## Recovery and refusal

The recovery capsule is create-once root-owned state, not cleanup authority.
The runner first reconciles an exact prior capsule/publication state. A
supervised run may perform one start-or-recover launch followed by at most one
recover-only launch. Missing claims, foreign resources, identity drift,
ambiguous publication prefixes, expired unreserved authority, or incomplete
terminal absence fail closed.

Never delete or normalize an ambiguous journal, receipt, claim, runtime
publication, capsule, or disposable resource by assumption. Diagnose it under
the same global lock and exact identities.

## Evidence classification

- Phase 8G: current application/runtime and chat-deletion disposable evidence;
  not installation or activation evidence.
- Phase 9J package verification: static repository evidence only.
- Phase 9J disposable Linux receipt: install/recovery/empty-rollback evidence
  for fixed disposable resources only; never production activation evidence.
- Production: remains unchanged and activation-blocked until Phase 10 receives
  separate authority and proves the full installed-to-consumed chain.

Chat-erasure validation remains chat-only. Accounts and structured LifeSwitch
records are outside the deletion graph.
