# Memory V1 Pattern Review/Apply V5.1

Status: clone-tested design; not installed in production; no retrieval or prompt influence.

## Purpose

This phase turns a deterministic pattern proposal into governed relational state. It does not infer identity, infer causality, create a scalar truth score, create a scalar salience score, write Qdrant, or affect an answer.

## Authority boundary

- The caller never supplies `owner_user_id`; every API derives it from the authenticated `app.user_id` context.
- `brains_app` has function execution only and no direct access to pattern tables.
- `memory_v5_epistemic_writer` is `NOLOGIN`, `NOINHERIT`, and `NOBYPASSRLS`.
- Every owner table uses forced RLS and composite owner foreign keys.
- Every observation must still belong to the actor, point to active evidence, match its immutable observation and evidence hashes, and resolve to the proposed subject/object entities.

## Review boundary

The proposal is closed JSON. Stable pattern identity is derived from:

`pattern_kind + subject_entity_id + secondary_entity_id + predicate_family`

The review manifest also binds the exact proposal hash, observation manifest, decision, reviewer, review reasons, and current pattern-head state.

Only `recurrence` and `persistence` may receive automatic system authorization. Automatic authorization additionally requires a trusted owner-self subject, no secondary entity, at least three occurrences, at least two independent episodes, at least two temporal buckets, and no counterexample. Transition, trend, co-occurrence, sequence, third-party, and counterexample-bearing patterns require human review.

Rejected and deferred reviews are durable audit outcomes but cannot be applied. An authorized review becomes stale if its observation/evidence hashes or the current pattern head changes. Only the latest review decision for a pattern key may be applied.

## Apply boundary

Apply uses a transaction-scoped advisory lock over `(owner, pattern_key)`, revalidates the review, and checks a second apply manifest. A successful first apply writes:

- one stable pattern head if none exists;
- one immutable numbered pattern revision;
- one immutable link for every reviewed observation;
- one immutable apply event; and
- one immutable request/replay record.

Only `current_revision_id`, `revision_number`, and `updated_at` may change on a pattern head, and only through a one-revision controlled transition. Replaying the same request writes zero rows. A reused request ID with a different manifest is rejected.

## Storage and later use

Postgres remains canonical. Qdrant, retrieval ranking, and prompt injection are explicitly outside this phase. Later projection work may index identifiers, but every candidate must be revalidated against these owner-scoped Postgres records before influence.
