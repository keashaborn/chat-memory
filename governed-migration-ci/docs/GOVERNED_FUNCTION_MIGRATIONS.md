# Governed function migrations

`governed-function-migration-package-v1` is a separate, fail-closed lane for replacing preexisting owner-isolated PostgreSQL APIs. It does not change `governed-migration-package-v1` or its table-only SQL allowlist.

The static validator accepts only manifest-bound `CREATE OR REPLACE FUNCTION` files using SQL or PL/pgSQL, `SECURITY DEFINER`, and an exact fixed safe search path: empty, `pg_catalog`, or `pg_catalog,memory`. Each replacement binds its prior and expected `pg_get_functiondef` SHA-256, owner, language, volatility, parallel mode, strict/leakproof flags, execute ACL, forced-RLS dependencies, forward bytes, rollback bytes, and a forward-recovery procedure. Dynamic SQL, function-body DDL, external I/O capabilities, function creation, and grant widening are denied.

Static validation is necessary but not sufficient. A networkless PostgreSQL clone must prove that every function existed before application; prior definitions match; owners, security mode, search paths, ACLs, and forced-RLS dependencies are exact; rollback restores the exact baseline; and reapplication reproduces the same expected state. Production execution remains a separately authorized transaction.
