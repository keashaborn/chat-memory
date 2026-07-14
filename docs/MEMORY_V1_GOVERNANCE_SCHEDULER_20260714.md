# Memory V1 governance scheduler — 2026-07-14

## Scope

- Owner allowlist: `557ea042-cb82-48f8-9429-472e96c957ef` only.
- Review manifest: `ops/manifests/memory_v1_synthetic_review_20260714.json`.
- Canonical manifest SHA-256: `09614a75f93b4c02dcb4d3a8b35b75454866a18eb8b09ee4d5486001545c6e03`.
- Reviewed inventory: 18 claim candidates and 1 preference candidate.
- Decisions: 10 claim approvals, 8 claim rejections, 1 preference rejection.
- The rejected `blocked lately` candidate remains immutable evidence and is not durable memory.
- The account-wide delete error is deferred to the audited erasure design; this change does not implement deletion.

## Controlled path

1. A hash-locked review changes a generic candidate to `approved` or `rejected` and appends `memory.governance_review_event`.
2. An approved candidate or accepted specialized review enqueues one idempotent `memory.governance_job`.
3. The allowlisted worker claims jobs with `FOR UPDATE SKIP LOCKED` and a short lease.
4. Claim promotion requires an active registered predicate and active evidence.
5. An exact canonical key links evidence to the existing claim. It does not create a duplicate claim.
6. A different value for a cardinality-one predicate is blocked as `contradiction_requires_review` unless the candidate explicitly and validly names a superseded claim.
7. Successful claim application transactionally enqueues a salience job keyed to the candidate hash.
8. A daily bucket adds at most one additional salience check per active claim.

Preference and project jobs call the existing accepted-review apply functions with deterministic request IDs and optimistic revision locks.

## Salience v1

Salience is retrieval priority, not truth. The job does not change claim status, confidence, evidence stance, or assessments.

The deterministic score combines:

- 50% stored importance;
- 30% independent supporting-source count, saturated at three independent sources;
- 20% evidence recency with predicate-specific half-life.

Scores are clamped to `0.150..1.000`, rounded to three decimals, and written only when the absolute change is at least `0.010`. A write appends a claim revision and refreshes the projection outbox.

## Clone verification

- Review replay: zero writes.
- Promotion: 10 completed, 0 errors.
- Salience: 10 completed, 0 errors.
- Immediate daily replay: zero jobs enqueued.
- Exact duplicate: linked new evidence to the existing claim; claim count unchanged.
- Cardinality-one contradiction: job blocked; conflicting claim not created.
- Transient candidate: rejected; no durable claim.
- Salience changed revision history but did not create a second truth assessment.
- Different actor: zero claims, jobs, or review events visible under forced RLS.
- `brains_app`: no delete privilege on governance tables.

## Production controls

- Install migration as PostgreSQL role `sage`.
- Run the review manifest once with the authorized canonical hash.
- Keep `MEMORY_V1_GOVERNANCE_OWNER_IDS` restricted to the synthetic account.
- The systemd service requires `--process`; manual invocation without that flag is read-only.
- Do not broaden the owner allowlist until cross-account trace review is complete.
