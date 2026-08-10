# Governed Memory Phase 4 activation boundary

## Current state

This is an inactive production-integration candidate. It is built and tested,
but it is not installed, enabled, started, routed, or authorized for a pilot.
No production PostgreSQL database, Qdrant collection, systemd unit, credential,
firewall rule, provider request, or activation marker is created by Phase 4.

The checked-in release guard currently refuses both resource creation and
cleanup. Creation returns `activation_blockers_open`; cleanup returns
`durable_pilot_marker_unavailable`. Both decisions contain an empty action
plan and execute zero commands.

## Exact candidate boundary

On the seebx backend:

- the candidate HTTP bind is `172.31.32.171:8091`;
- the only intended frontend source is Verbal Sage `172.31.43.160/32`;
- the canonical successor PostgreSQL target is a new daemon at
  `127.0.0.1:55432`, database `governed_memory`;
- the derived Qdrant target is a new daemon at `127.0.0.1:6343`, physical
  collection `governed_memory_9a54cf123493_000001`, alias
  `governed_memory_active`, dimension 3072, distance `Dot`; and
- the existing conversation database `memory` is used only for the atomic,
  content-free `memory_ingest_private.memory_ingest_outbox` bridge through the
  existing application login `brains_app`.

The production PostgreSQL daemon on port 5432 is not a successor claim store.
Its observed `log_parameter_max_length=-1` fails the successor requirement of
`0`. The production Qdrant daemon on port 6333 is not a successor vector store.
No existing database, collection, volume, mount, snapshot, vector, claim,
review, preference, job, attachment, or compatibility row may seed the new
stores.

The pinned Qdrant v1.11.0 digest in `compose.candidate.yaml` is retained only
for disposable Phase 4 compatibility validation. It is not approved for a
persistent pilot. Before persistent creation, a current immutable Qdrant image
must receive a fresh security review and the full compatibility suite must pass
against that exact digest. The compose contract and guard must then be changed
in an explicitly authorized candidate.

## Candidate-owned Python

The validated runtime is addressed by both the runtime-lock SHA-256 and the
canonical successor-package source-tree SHA-256:

```text
/tmp/governed-memory-phase4-runtime-94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365-9225cb61154b7b054fa973ac025a1f41e83a5fc9d8d7bc86069b85e8af3ee57b/bin/python
```

It is a copied, non-symlink CPython 3.12.3 executable. The environment contains
the project plus exactly the 19 hash-locked runtime dependencies. It contains
no pip, setuptools, wheel, user-site access, or `/opt/chat-memory` import path.
The build backend is separately locked to the official setuptools 84.0.0 wheel.
`/opt/governed-memory/current` is a future activation target only; Phase 4 does
not create or write it.

The builder is create-only and fails if its dependency-lock-and-source-addressed
output exists. The checked-in content-free build receipt binds the exact runtime
path, interpreter, lock hashes, source-tree hash, and project wheel. It accepts
only an absolute, current-user/current-group wheelhouse with mode `0700`, exact
hash-set equality to the 19 runtime-lock wheels, no extra entry or symlink, and
no group/world-writable wheel. It builds only
`rag_engine.governed_memory`, installs with no dependencies beyond the runtime
lock, removes pip, verifies the final distribution inventory, and emits a
content-free receipt.

## Authentication boundary

The owner remains the UUID in the verified asymmetric Supabase JWT `sub`.
Caller headers, paths, queries, bodies, profile metadata, and application
metadata cannot establish authority. The active candidate would also perform a
bounded HTTPS `GET /auth/v1/user`: no redirects, no proxy inheritance, a five
second transport bound, a 64 KiB body bound, and exact returned `id == sub`.
The adapter never logs or persists the bearer token, API key, or response body.

A successful `/auth/v1/user` request is not claimed to guarantee immediate
signout revocation. Activation requires an explicit decision and proof for
checking the signed `session_id` against `auth.sessions`, including outage,
revocation, key-rotation, and durable content-free provenance behavior.

## Conversation and attachment boundary

Capture defaults to `off`. A future pilot requires a server-owned external
owner allowlist. Only a new user-authored chat row inserted after cutover can
enqueue, and enqueue must occur in the same transaction as that exact row.
There is no historical scan or backfill. The worker has no `SELECT` privilege
on base chat or attachment tables; its lease-token reader can return only the
one exact bound user message. Assistant turns, voice transcripts, web results,
no-store turns, old chats, and attachment content are ineligible.

The attachment UI and storage capability remain available independently.
Neither existing nor new attachment bytes enter release-1 Memory.

## Open activation blockers

All blockers below must be closed with evidence in one scoped activation
candidate before any persistent pilot:

1. Calibrate the semantic retrieval threshold on an authorized holdout.
2. Mount live Supabase signing-key, service, and user-check configuration.
3. Decide and prove strict `session_id`/`auth.sessions` revocation semantics.
4. Create a fresh isolated persistent PostgreSQL daemon and database.
5. Security-review a current immutable Qdrant digest and pass compatibility.
6. Prove a source firewall that admits only `172.31.43.160/32` to port 8091.
7. Decide and prove TLS termination or equivalent private transport.
8. Implement and authorize a credential-free-at-rest role/bootstrap procedure.
9. Install, configure, and prove the separate HTTP service.
10. Authorize and activate the atomic conversation-capture membership.
11. Implement the real provider adapter without automatic unknown retry.
12. Implement the real 3072-dimension embedding adapter.
13. Implement the real Qdrant projection/retrieval worker adapter.
14. Validate the authenticated Verbal Sage owner-lifecycle frontend.
15. Implement and validate an exact owner-scoped claim fact-detail response that
    exposes the admitted subject and object needed for display and correction.
16. Authorize one exact pilot owner and the bounded pilot scope.
17. Implement a durable content-free monotonic `pilot_ever_started` marker.
18. Prove owner-scoped legacy Memory read, write, and shadow paths quiesced.

The final legacy-quiescence proof is required because production still has
multiple old Memory active/shadow feature flags. Phase 4 does not alter those
flags or production units. A pilot without owner-scoped quiescence could mix
old and successor outputs and invalidate the evaluation.

## Authorized activation sequence

Each step is a separate checkpoint. Do not proceed on intent or source-only
evidence.

1. On the seebx backend, verify hostname, production HEAD/status, active
   services, occupied ports, existing store identities, and legacy feature
   flags. Save a content-free pre-state receipt.
2. Approve an immutable current Qdrant digest, replace the disposable digest,
   and rerun unit plus full disposable validation using the lock-addressed
   candidate Python.
3. Approve the exact persistent PostgreSQL/Qdrant targets and credentials
   separately. Change the hard-refusing create guard only in that authorized
   candidate. Existing-target detection must refuse rather than adopt.
4. Create the two new empty daemons. Prove PostgreSQL UTF-8, both parameter-log
   settings equal `0`, exact roles/grants/forced RLS, zero user rows, and no
   legacy mount or snapshot. Prove Qdrant has only the exact empty collection
   and alias, zero points, dimension 3072, and `Dot` distance.
5. Install the candidate-owned wheel/runtime and HTTP unit. Keep mode `off`.
   Prove source, wheel, installed files, configuration, disabled state, and
   zero listener before changing mode.
6. Install the exact source firewall and private-transport boundary. From
   Verbal Sage, prove authenticated access; from another source, prove denial.
7. Mount Supabase configuration and prove JWT, JWKS rotation, live-user outage,
   owner mismatch, revoked-session, and cross-owner negative cases.
8. Implement and validate the provider, embedding, and Qdrant workers against
   synthetic/disposable inputs. Do not use a real provider before its separate
   authorization.
9. Quiesce legacy Memory only for the authorized pilot owner. Prove no legacy
   read, write, shadow, job, card, or vector path is invoked for that owner.
10. Authorize one owner, at most 20 post-cutover user messages, at most 24
    hours, worker concurrency one, and one generation call per exact attempt.
11. Start the pilot with an immutable content-free receipt. Stop on any
    authority ambiguity, owner leak, legacy/attachment read, unknown provider
    dispatch, projection mismatch, or firewall failure.
12. Stop and review the pilot before expansion. No automatic general rollout.

## Cleanup and retirement

Phase 4 does not provide an executable destructive cleanup path. Caller-supplied
zero counts cannot prove that a pilot never started, because content rows may
already have been purged. Until a monotonic durable marker exists, cleanup must
remain hard-refused even when PostgreSQL and Qdrant appear empty.

After a successful pilot, old Memory code and stores are retired in bounded,
approved batches: disable exact owner paths, observe, preserve one immutable
recovery manifest, then delete only explicitly named objects with a content-free
receipt. Never use broad project teardown, wildcard/prefix selection, SQL
cascade, or deletion based only on a naming convention.
