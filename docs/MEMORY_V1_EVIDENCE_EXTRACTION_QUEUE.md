# Memory V1 evidence extraction queue

The queue receives only `eligible` rows produced by
`memory.plan_owner_evidence_intake_v1`. Enqueueing is a terminal intake action:
the same transaction creates an append-only intake receipt with outcome
`dispatched`, the pending extraction job, and its initial `queued` event.

Exact replay returns the existing job and writes nothing. Missing or conflicting
job/receipt pairs fail closed. The intake plan automatically stops returning a
dispatched evidence row because the receipt uses the same
owner/evidence/selector-version uniqueness boundary as empty and skipped
outcomes.

`brains_app` has read-only table access. It can enqueue only through
`memory.enqueue_owner_evidence_extraction_v1(...)`, which derives the owner from
`app.user_id` and revalidates the current selector plan. The function is owned
by `memory_extraction_queue_maintainer`, a `NOLOGIN`, `NOINHERIT`,
`NOBYPASSRLS` role. Queue, event, evidence, and intake tables use forced RLS.

The dispatcher may record terminal intake decisions and enqueue eligible rows
in one all-owner transaction. Deferred rows remain untouched. The dispatcher
contains no model caller and cannot create candidates, claims, projections, or
Qdrant points. A model-consuming worker is a later, separately tested phase.
