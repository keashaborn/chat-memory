# Memory V1 V5 extraction call ledger

Status: production-clone tested. Schema, worker, service, and timer are not yet
installed or enabled in production.

The ledger closes the gap between queue leasing and external extraction. A call
unit is reserved in the same database transaction that leases one owner-scoped
job. The reservation counts against quota even if the process crashes before it
can record whether the provider was reached.

## Enforced controls

- Owner is derived from `app.user_id`; the mutation functions take no owner
  argument.
- Only `brains_app` can execute the two restricted functions.
- Direct `brains_app` table writes are denied; the maintainer role is `NOLOGIN`,
  `NOINHERIT`, and `NOBYPASSRLS`.
- The ledger is forced-RLS and append-only.
- Job claim and call reservation are one transaction under an owner/route lock.
- One reservation can have at most one completion.
- A successful completion must match an already-persisted immutable V5 packet
  and a `review_required` job.
- Reports and ledger rows contain identifiers, hashes, counts, routing outcomes,
  and rejection codes only. They contain no query text, evidence prose, prompt
  content, or extracted claim prose.
- Default runtime ceiling is one job per cycle, 12 reserved call units per
  rolling 24 hours, and an open circuit after three consecutive rejections.
- Provider requests remain `store=false` with SDK retries disabled.

## Activation boundary

The service and timer files are source artifacts only. Installing the schema and
deploying the worker do not enable external calls. Installing and enabling the
timer is a separate boundary because it transmits real owner evidence to OpenAI.

This phase still writes no candidates, governed claims, durable projections,
Qdrant points, retrieval output, prompts, or frontend state.
