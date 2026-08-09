# Governed function creation

`governed-function-creation-package-v1` is the separate fail-closed lane for
adding a new owner-isolated PostgreSQL API. It does not alter the table-only
migration contract or `governed-function-migration-package-v1`, which remains
replacement-only.

The static validator accepts only manifest-bound `CREATE FUNCTION` statements
using SQL or PL/pgSQL, `SECURITY DEFINER`, and an exact fixed safe search path.
`CREATE OR REPLACE`, `IF NOT EXISTS`, dynamic SQL, function-body DDL, external
I/O capabilities, PUBLIC execution, and non-exact rollback are denied. The
rollback artifact must contain only `DROP FUNCTION` for the exact signature.

Each creation binds the expected `pg_get_functiondef` SHA-256, owner, language,
volatility, parallel mode, strict/leakproof flags, execute ACL, forced-RLS
dependencies, forward bytes, rollback bytes, and forward-recovery procedure.
The append-only registry binds the complete package hash.

A networkless PostgreSQL clone must prove prior absence, exact applied state,
rollback to absence, deterministic reapplication, and final rollback to
absence. Production application remains a separately authorized transaction.
