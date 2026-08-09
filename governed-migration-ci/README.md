# Governed migration full-chain CI candidate

This candidate defines the first future-only governed migration contract. It
does not authorize or execute any of the 667 legacy SQL sources. The Step 3
catalog ledger is the required baseline identity; PostgreSQL remains live
authority and Qdrant remains derived.

The reviewed CI fixture uses synthetic schema names, roles, and zero data rows.
The harness starts one digest-pinned PostgreSQL 16 container with no network,
host port, or host volume. It proves apply, catalog/RLS/grant verification,
exact rollback, reapply, replay, atomic failure, crash recovery, timeout,
concurrency denial, stale-lock recovery, append-only audit, and exact teardown.

Future governed migrations use `forward.pgsql` and either `rollback.pgsql` or a
reviewed forward-recovery document. The historical `.sql` namespace stays
frozen. A new `.sql` file fails the Step 3 baseline and this validator; every
new `.pgsql` file must belong to exactly one canonical governed package.

Nothing in this directory connects to production PostgreSQL or Qdrant. The
only database executor is `tools/full_chain_harness.py`, which accepts a
CI-only fixture and an absolute Docker executable and creates a labeled,
resource-bounded, networkless disposable container.

Function authority is split into two non-overlapping contracts. Existing
functions may be changed only through `governed-function-migrations` and its
replacement-only validator. New functions may be introduced only through
`governed-function-creations`, the separate create-only registry, and
`tools/governed_function_creation.py`. The create-only lane requires proven
prior absence, exact `CREATE FUNCTION` bytes, a fixed SECURITY DEFINER search
path, exact non-PUBLIC execute ACLs, forced-RLS dependencies, an exact-drop
rollback, deterministic reapplication, and final restoration to absence.
Neither lane can accept the other lane's operation.
