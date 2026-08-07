# Full-chain migration, rollback, and recovery CI

## Authority and legacy boundary

PostgreSQL is live schema authority. The Step 3 ledger is intended-history and
baseline authority for future governed migrations. Qdrant is derived and is
represented only by sanitized metadata where a migration explicitly changes a
derived-index contract.

The 667 historical repository SQL records remain inventory evidence: 491 are
unverifiable, 173 are source-only, and 3 are duplicated. Their path/blob/hash
set is frozen. CI never orders, applies, rolls back, or marks them executed.
Changing or deleting one fails.

Two Chat Attachments `.sql` sources entered the repository and were deployed
under separately audited production leases after that baseline was frozen but
before this Memory candidate reached repository reconciliation. The closed
`registry/external-deployed-sql-v1.json` records only those exact paths, Git
blobs, SHA-256 values, source-projection hashes, production commit, and lease
events. The validator duplicates the authorized binding in code, requires the
adopted commit to be an ancestor, and verifies the same blobs at both that
commit and the current commit. The registry is append-only after creation,
closed at exactly two records, read-only, and grants no execution authority.
Changing either source, changing its evidence, or adding any other `.sql`
source fails.

The frozen ledger also pseudonymized two preexisting semantic-compiler source
paths while the repository retained their subject-named paths. Their Git blobs
and SHA-256 values are byte-identical. The closed
`registry/legacy-sql-path-aliases-v1.json` binds only those two ledger-path to
repository-path identities, their unchanged blob/hash and ledger-projection
hash, and the exact adopted repository commit. The ledger records remain
unchanged; the alias is used only while comparing Git inventory. It grants no
execution authority. A changed path, changed byte, third alias, surviving old
path, or additional `.sql` source fails.

Future governed sources still use the distinct `.pgsql` namespace. Every `.pgsql`
file must be referenced exactly once by a canonical package. Production
packages live under `governed-migrations/<migration-id>/`; the only package in
this candidate is synthetic and `ci_only=true` under `fixtures/`.

## Package and execution semantics

Each package binds one immutable migration ID to exact manifest, forward, and
recovery bytes; dependencies; lane; execution owner; Step 3 ledger/catalog
baseline; transaction and timeout policy; one advisory-lock key; explicit
object/RLS/policy/grant fingerprints; and append-only execution evidence.
Package hashes are recorded in the append-only governed registry. Reusing an
ID with changed bytes fails. A superseding migration receives a new ID.

Version 1 accepts only transaction-required, schema-only changes. It denies
data writes, destructive forward actions, role or database administration,
extension loading, client shell escapes, external programs, files, foreign or
cross-database access, privilege escalation, RLS weakening, grant widening,
and nontransactional operations. Recovery must be either an exact rollback or
a separately reviewed forward-recovery procedure.

The future production execution ledger is append-only and hash-chained by run,
task, actor, package, baseline, event ordinal, state fingerprint, and prior
event hash. This candidate creates that design only inside the disposable CI
database. It creates no production relation and marks no migration applied.

## Full-chain sequence

1. Verify the Step 3 manifest and schema ledger, freeze the 667-record legacy
   inventory, verify the two closed identity-only path aliases and two closed
   external-deployment records without granting execution authority, and
   reconcile every governed package and `.pgsql` source.
2. Verify the digest-pinned image and require the exact container name absent.
3. Start PostgreSQL with `--network none`, no port/volume, tmpfs data, private
   synthetic credentials, memory/CPU/PID bounds, reduced capabilities, and a
   unique run label.
4. Bootstrap only synthetic roles plus the append-only CI execution ledger.
5. Validate the empty synthetic target and bind it to the Step 3 ledger and
   catalog evidence hashes.
6. Prove concurrent runner denial and that session-owned stale advisory locks
   disappear when the owning session exits.
7. Apply the reviewed fixture in one bounded transaction; verify exact object,
   owner, RLS-force, policy-expression, and grant fingerprints.
8. Roll back and require exact target restoration; reapply and require the
   identical post-state; replay and require no schema change.
9. Inject a transaction failure and require zero partial objects; simulate a
   crash after commit but before completion evidence and recover only from an
   exact post-state fingerprint; inject a timeout and require unchanged state.
10. Reject update/delete against the execution audit, verify its hash chain,
    then identity-check and remove the one labeled container. Require absence.

## CI activation and rollback

Activation is a later fresh-leased commit that adds only the reviewed
`governed-migration-ci/` subtree and the exact Auditability workflow edit. It
does not apply a production migration, create a production execution ledger,
change PostgreSQL/Qdrant/services/configuration, deploy, restart, or push.

The workflow first retains all Step 3 gates, then runs warning-as-error Step 4
tests, the Step 4 manifest, repository/package reconciliation, and the
networkless full-chain harness. The PostgreSQL image is pinned by digest.

Rollback is a fresh-leased revert of that one activation commit. GitHub has not
been pushed in this phase, so pre-push rollback is local. If CI container
cleanup fails, preserve the exact label/name evidence and remove only that
verified container; never use broad Docker cleanup.

## Remaining limitations

This harness validates the current catalog ledger identity but does not rebuild
all 11,967 production objects: Step 3 intentionally retained no schema dump.
The disposable schema is a contract fixture, not a production clone. A future
production runner, execution-ledger DDL, operator identity mechanism, and
forward-only recovery approval flow remain separate reviewed work. PostgreSQL
advisory locks coordinate compliant runners; they cannot stop an actor with
direct database credentials from bypassing the governed interface.
