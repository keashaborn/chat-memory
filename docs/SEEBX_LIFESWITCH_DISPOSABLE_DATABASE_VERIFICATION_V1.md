# SeeBx LifeSwitch disposable database verification v1

Status: candidate verification evidence; no deployment or database-change authority

Evidence date: 2026-08-21 America/Chicago

Application candidate commit: `246be57499b5b7b17df66e2474957b44a7300f06`

Canonical verifier: `scripts/verify_lifeswitch_disposable_restore_v1.py`

Verifier SHA-256: `beb7afbf9d72d34e461ddf06d2ebd0895c69de13f1bb7d1caad4ccac608d2b73`

## Boundary

The verifier reads the running `lifeswitch-postgres-current` container and the
`lifeswitch` database through the administrative `lifeswitch_bootstrap` role.
It never reads a password, opens a host TCP connection, or writes to the source
database. It creates one uniquely named database from `template0`, restores a
custom-format dump in one transaction, verifies the clone, drops the clone on
success or failure, and independently proves that no disposable database
remains.

All artifacts are root-owned and mode 0600. The database dump contains row
data and is therefore protected; the manifests, receipt, and diagnostic output
are content-free. Diagnostics name only database objects and changed field
names; they never emit row values.

## Verified assertions

Run `20260822t0250z` passed all gates:

- exact source/restored manifest equality across schemas, semantic ACLs,
  relations, owners, RLS/force-RLS flags, view definitions, functions,
  policies, extensions, and exact table row counts;
- 48 tables and 5,590 exact rows;
- `lifeswitch_app_login` remains a login role, non-superuser,
  non-`BYPASSRLS`, and a member of the expected application groups;
- five retained owners across 33 protected surfaces, producing 165 exact
  administrator-versus-application visibility comparisons;
- three cross-owner writes denied;
- direct application access to both snapshot tables denied;
- catalog read succeeds;
- delegated reads and the People dependency are both explicitly disabled and
  therefore fail closed;
- the disposable database was dropped and a separate catalog query found zero
  `lifeswitch_verify_%` or `lifeswitch_diag_%` databases;
- production source writes, source changes, and service restarts were all zero.

## Evidence bindings

- Retained evidence directory:
  `/var/backups/seebx-cleanup/lifeswitch-disposable-verify-v1/20260822t0250z`
- Database dump SHA-256:
  `78f329ec02cb88e85a3099ad396fd308239b5dec159deedd39d8a749c55c5807`
- Source manifest SHA-256 recorded by the receipt:
  `fd71e52ac0825661df33f36c9bff1b595e0cf333221b8bd0ba090ad6dac4ad67`
- Source manifest file SHA-256:
  `1bc75e31d70c0ae39b16e339d4e395e791585078935c7b2cdc5a96445f329b6d`
- Verification receipt SHA-256:
  `cca916ee5f460682d98660fcb6be64fe1e5d650da7dfad471744c7a7902c59c9`

Eight failed development runs were inspected, contained no passing receipt,
and were removed by exact path after this passing evidence was retained.

## What this does not prove

This verifies that the current isolated LifeSwitch database can be backed up,
restored, and exercised under its present owner/RLS contract. It does not prove
a clean installation from migrations, delegated People behavior while enabled,
authenticated frontend bearer forwarding, application-route behavior against
the clone, release installation, deployment, cutover, or rollback of a release.
Those remain separate gates.

The former `scripts/verify_lifeswitch_isolated_postgres_v1.py` connected
directly to live credentials, checked only a subset of the surfaces, and did
not prove backup, restore, manifest equality, or cleanup. It had no callers and
is retired without a compatibility wrapper. The disposable verifier is the
single canonical database verification path.
