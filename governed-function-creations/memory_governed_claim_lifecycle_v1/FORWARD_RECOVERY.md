# Governed claim lifecycle V1 forward recovery

This package creates only two owner-scoped `SECURITY DEFINER` functions. It does
not alter tables, delete claim history, dispatch projection work, or call
Qdrant. The semantic-delete operation appends the existing governed retraction
transition and creates a delete outbox item held at `available_at = infinity`.

Before forward execution, verify the exact package and SQL hashes, the expected
functions are absent, the execution role is `sage`, and every declared relation
has forced RLS with the package-declared owner and policies. Apply both forward
files in one transaction using advisory lock `724613047141`,
`lock_timeout = 1000ms`, and `statement_timeout = 15000ms`.

After forward execution, verify each function's exact definition hash, owner
`memory_v5_writer`, fixed `pg_catalog` search path, `SECURITY DEFINER` flag,
PUBLIC denial, and `brains_app` execute grant. Run the owner, cross-owner,
stale-state, exact replay, semantic-delete, held-outbox, rollback, and
deterministic-reapply fixtures in a disposable PostgreSQL 16 clone.

For recovery, stop before rollback if either function definition, owner, ACL, or
declared dependency differs from the package. Apply the two rollback files in
one short transaction. Rollback drops only the wrapper functions; it does not
erase any claim, revision, assessment, operation receipt, or outbox history
previously created through them. Reapply the exact forward files and repeat the
full verification before any later activation request.
