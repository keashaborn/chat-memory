# Governed Memory clean successor — Phase 6B inactive candidate

This repository contains the single current governed-Memory source candidate.
It is not production activated: no successor service is installed, enabled,
started, routed, or connected to a production successor store. No production
PostgreSQL or Qdrant resource has been created, and no real provider call has
been authorized or made.

The successor requires CPython 3.12.x. A prior Phase 5 runtime was built with
CPython 3.12.3 and 19 hash-locked runtime packages. Its checked receipt binds
the historical source SHA-256
`af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b`
and wheel SHA-256
`58146af4097400097b1312011c591d1878904f7ac5709b0fdecd57da3fc0f8e4`.
Phase 6B changed the source and migrations, so that runtime and disposable
receipt are stale evidence and cannot validate this candidate. A new sealed
runtime build and full disposable proof are pending. Nothing is activated.

PostgreSQL is canonical. Qdrant is a disposable, derived index rebuilt only
from current PostgreSQL claims and applied projection receipts. Supabase remains
the account authority; accounts are not copied. Legacy claims, cards, vectors,
jobs, reviews, preferences, compatibility state, and attachment content are not
imported.

## Implemented source surfaces

- Owner authority comes only from the verified Supabase JWT `sub`. A signed
  `session_id` is mandatory. The exact `auth.sessions` RPC contract is staged,
  but it is not installed or live verified.
- The owner-scoped claim-list and claim-detail routes are implemented and
  candidate-tested. Their older Phase 5 proof is historical; fresh Phase 6B
  disposable migration and HTTP lifecycle revalidation is pending.
- Strict provider and 3072-dimension embedding adapters are fake-tested. A
  content-free durable request marker must commit before an embedding HTTP
  call; a marked request is never automatically resent. They have made zero
  external provider calls and have no production authorization.
- The exact Qdrant adapter is fake-tested for configured alias/physical target,
  3072-dimensional `Dot`, six payload indexes, owner-filtered bounded search,
  ambiguous-write readback, and verified deletion. Qdrant v1.19.0 is pinned at
  digest `057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`;
  real disposable compatibility passed. Persistent Qdrant remains unapproved.
- The content-free monotonic pilot marker exists as migration candidate 0004.
  Insert, replay, conflict refusal, readback, and guarded rollback passed on
  disposable PostgreSQL; it is not production-applied.
- The one-shot worker now has inactive two-database composition, exact
  PostgreSQL RPC repositories, exact loopback Qdrant transport, a PostgreSQL
  session advisory lock, and a persistent content-free three-lane fairness
  cursor. These surfaces have focused unit/static proof only; a fresh
  disposable two-database runtime proof is pending.
- Messages with an attachment row are excluded at enqueue, lease, and exact
  source read. Context-required messages perform one content-free successor
  receipt lookup for crash recovery, then terminalize by two exact marks when
  fresh or one after recovery from a completed first mark. They perform zero
  successor writes, provider, embedding, or vector calls.
- Capture is owner-serialized and limited to 20 outbox rows across all states
  in a rolling 24-hour window. Exact replay consumes no additional slot; the
  limit returns a typed content-free result so the chat transcript can commit.
- Qdrant failure before embedding dispatch remains retryable. Failure after a
  durable embedding marker is terminal without a second embedding call; the
  canonical PostgreSQL claim remains, and later projection for that claim is
  blocked until an explicit reconciliation workflow safely repairs it.
- Calibration requires independently expected artifact and approval-receipt
  SHA-256 values. The checked-in artifact is unapproved, so semantic retrieval
  remains off and no semantic retrieval-score threshold is activated.
- Verbal Sage frontend candidate `6d80ba` was built but is undeployed;
  authenticated visual QA is pending.

## Current path

```text
post-cutover user message
  -> content-free conversation outbox
  -> deterministic eligibility
  -> one durable extraction attempt
  -> explicit owner review
  -> PostgreSQL claim and immutable revision
  -> projection outbox
  -> derived Qdrant candidates
  -> PostgreSQL revalidation
  -> answer binding
```

Qdrant cannot supply owner identity, fact content, policy, lifecycle, current
revision, or rebuild truth. PostgreSQL reloads and revalidates every candidate
before rendering. Attachment storage remains available, but attachment content
is not release-1 Memory input.

Terminal proposal replay is bounded to the 30 days before terminal proposal
retention purge. After the content-bearing proposal is
removed, exact replay is unavailable and the API returns
`proposal_retention_purged` using content-free audit evidence rather than
reconstructing deleted proposal content.

## Proof boundary

The historical Phase 5 harness exercised disposable PostgreSQL and Qdrant with synthetic
identities, exact owner isolation, migration rollback/reapply, Qdrant 1.19.0,
pilot-marker semantics, all owner HTTP routes, zero external provider calls,
and complete invocation-resource cleanup. The attested pre-promotion candidate
was HEAD `699c80761065d19832d2c0f3b2b50342a5a8350c`, tree
`c5c13579ffa54da4f30fc198254c98b662c7029b`, and migration manifest
`2174711255ba55eeb2233703a0e3813a7d9e191b275cadc5959f7aaee3ab9b45`.
Metadata promotion changed the tree without rewriting those receipt facts.
Those facts do not bind the Phase 6B source or migrations.

Current focused tests do not prove disposable PostgreSQL execution, real
two-database composition, concurrent advisory locking, live Supabase session
revocation, real provider behavior, installed production routes, authenticated
frontend behavior, persistent Qdrant suitability, or production readiness.

## Activation and retirement

Production remains unchanged. Any authorized pilot must use a new empty
PostgreSQL database and new empty Qdrant collection with no old or unprocessed
data. Both checked-in systemd templates default off, have no install target,
and are not installed.

Old services, stores, source, tests, and compatibility routes are not deleted
by this candidate. After a proved pilot, retire them in explicit batches:
disable exact owner paths, observe, preserve one recovery manifest, and delete
only approved targets with a content-free receipt.
