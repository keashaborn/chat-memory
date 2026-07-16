# Memory V1 owner-scoped evidence intake selector

The selector operates only on active evidence that has no existing processing
reference. A processing reference includes evidence-ingest audit rows, artifact
links, claim/candidate links, entity links, preference/project links, V5
relational staging, observation entailment, and durable preference links.

The read-only plan returns no evidence text. It returns evidence IDs, content
hashes, deterministic outcomes, routes, reason codes, and sanitized upstream
job metadata.

Outcomes:

- `eligible`: no upstream job or durable processing reference exists.
- `deferred`: an upstream job is pending, processing, or retryable after error.
- `empty`: content is empty or an upstream job completed with zero candidates.
- `skipped`: the content hash is invalid, upstream explicitly skipped or held
  the row for review, or upstream candidate IDs exist without durable links.

Only `empty` and `skipped` are terminal for a selector version. They may be
recorded in the append-only `memory.evidence_intake_terminal` ledger. Exact
replay is zero-write. `eligible` and `deferred` are never written to that ledger.

All planning and recording derive the owner from `app.user_id`, require a
`brains_app` session, run under a `NOLOGIN`, `NOINHERIT`, `NOBYPASSRLS`
maintainer, and query tables protected by forced RLS. The selector performs no
model calls and does not create candidates, claims, projections, or Qdrant
points.
