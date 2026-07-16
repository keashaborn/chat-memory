# Memory V1 evidence privilege boundary

`memory.evidence` remains the immutable, owner-scoped evidence store. Existing
rows are retained even when they do not yet have a candidate, claim, preference,
or project link; those rows are intake backlog, not duplicate data.

`brains_app` may read owner-visible evidence under forced RLS but may not insert,
update, or delete evidence directly. New evidence is recorded only through
`memory.record_owner_evidence_v1(...)`. The function derives the owner from
`app.user_id`, rejects missing actor context, separates identical source keys
between owners, returns the existing row on exact replay, and rejects changed
content or tombstoned source keys.

`memory_evidence_maintainer` owns the controlled writer. It is `NOLOGIN`,
`NOINHERIT`, and `NOBYPASSRLS`; it holds only the table privileges required by
the evidence lifecycle and writer functions.

The cleanup migration removes only direct `INSERT` from `brains_app`. It does
not delete or rewrite evidence, change retrieval, write Qdrant, or activate
prompt influence.
