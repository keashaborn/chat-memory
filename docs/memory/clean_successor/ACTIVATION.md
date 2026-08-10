# Governed Memory Phase 6B activation boundary

## Current state

The Phase 6B source candidate is inactive and not authorized. Neither systemd
template is installed or enabled. No production successor database, collection,
listener, route, firewall rule, capture membership, provider request, or pilot
marker row exists. The release guard returns `activation_blockers_open` for
create and `authorization_missing` for cleanup, with an empty action plan and
zero executed commands.

The prior Phase 5 CPython 3.12.3 runtime receipt binds historical source SHA-256
`af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b`
and wheel SHA-256
`58146af4097400097b1312011c591d1878904f7ac5709b0fdecd57da3fc0f8e4`.
The historical Phase 5 disposable harness passed against that older source.
Phase 6B changed runtime and migration bytes; a fresh source-bound runtime and
full disposable receipt are pending. The old receipt closes no current Phase
6B proof blocker and is not production activation evidence.

## Exact candidate targets

On the seebx backend:

- HTTP candidate: `172.31.32.171:8091`, with Verbal Sage
  `172.31.43.160/32` as the only intended source;
- canonical PostgreSQL: new `127.0.0.1:55432`, database
  `governed_memory`;
- derived Qdrant: new `127.0.0.1:6343`, collection
  `governed_memory_9a54cf123493_000001`, alias
  `governed_memory_active`, size 3072, distance `Dot`; and
- content-free conversation bridge:
  `memory_ingest_private.memory_ingest_outbox` in the existing conversation
  database through `brains_app`.

No existing database, collection, volume, snapshot, vector, claim, review,
preference, attachment, or compatibility row may seed the successor.

Qdrant v1.19.0 is pinned at immutable digest
`057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`.
The exact adapter is fake-tested, but real disposable compatibility against
that image also passed. The digest is not persistent-pilot approval.

## Implemented but inactive

- JWT `sub` is owner authority and signed `session_id` is mandatory. The staged
  Supabase `auth.sessions` RPC is not installed or live verified.
- Owner-scoped claim detail is implemented, candidate-tested, and proved on
  disposable PostgreSQL.
- Strict provider and 3072 embedding adapters are fake-tested. The exact
  Qdrant adapter is fake-tested and proved against disposable Qdrant 1.19.0;
  no real provider call has occurred.
- Migration 0004 implements a content-free append-only pilot marker, but it is
  proved only on disposable PostgreSQL and is not production-applied.
- The one-item worker has inactive exact two-database composition, RPC-only
  repositories, loopback Qdrant transport, exact target configuration,
  persistent three-lane fairness, and PostgreSQL session advisory locking.
  These are focused-test results only; fresh disposable and concurrent proof
  remain open.
- Calibration requires independent artifact and approval-receipt hashes. Its
  artifact is unapproved and retrieval remains off.
- Frontend candidate `35a684` is built, undeployed, and awaiting authenticated
  visual QA.

## Activation blockers

The machine-readable runtime manifest is authoritative for the current blocker
list. Before any persistent pilot, all of the following remain required:

1. Obtain explicit production activation and exact pilot-owner/scope authority.
2. Install and live-verify the mandatory `session_id`/`auth.sessions` RPC path,
   including revoked, missing, outage, rotation, and cross-owner cases.
3. Mount and verify live Supabase runtime credentials without exposing them.
4. Keep semantic retrieval off until an independently authorized calibration
   artifact is approved.
5. Build a fresh sealed runtime and prove the Phase 6B two-database worker,
   persistent fairness cursor, attachment exclusion, exact pilot identity, and
   cross-process singleton against fresh disposable stores.
6. Authorize any real provider/embedding validation separately; unknown
   post-dispatch outcomes remain terminal and are never automatically retried.
7. Create fresh isolated persistent PostgreSQL and Qdrant targets only after a
   scoped creation authorization and implement the production store-role
   bootstrap.
8. Prove port-8091 source firewall and private transport/TLS boundaries.
9. Install the HTTP and worker units in mode `off`, then prove their exact
    source, configuration, disabled state, and zero listeners.
10. Authorize and activate capture for one exact owner only.
11. Deploy frontend candidate `35a684` and complete authenticated visual QA.
12. Prove owner-scoped legacy Memory read/write/shadow paths are quiesced.
13. Authorize one owner, at most 20 post-cutover messages, at most 24 hours,
    and one generation call per exact attempt.
14. Implement and prove chat-deletion cancellation/erasure coordination.

## Authorized sequence

Each step is a separate checkpoint:

1. Capture seebx hostname, HEAD/tree, services, ports, store identities, and
   legacy flags in a content-free pre-state receipt.
2. Build a new runtime receipt that binds the exact sealed Phase 6B source,
   runtime/build locks, installed package, and wheel bytes; do not reuse the
   Phase 5 receipt.
3. Reproduce the full disposable proof if the sealed source, runtime, migration
   manifest, pinned images, or proof environment changes.
4. Review and authorize exact persistent targets and credentials. Existing
   target detection must refuse; adoption is prohibited.
5. Create empty stores and prove exact roles, forced RLS, zero user rows, exact
   Qdrant alias/collection/indexes, and zero points.
6. Install both dormant units, then prove firewall/private transport.
7. Prove live Supabase user and session authority plus owner-negative cases.
8. Prove two-database worker composition, persistent three-lane fairness, and
   cross-process concurrency one.
9. Deploy and visually validate the authenticated frontend.
10. Quiesce legacy paths for the authorized owner.
11. Record the monotonic pilot marker and start only within the approved bounds.
12. Stop on authority ambiguity, owner leakage, legacy/attachment reads,
    provider outcome uncertainty, projection mismatch, or firewall failure.

## Cleanup and retirement

The cleanup proof-blocker list is empty, but cleanup remains hard-refused because
no scoped cleanup authorization exists. The release guard returns
`authorization_missing` with no action plan; caller-supplied zero counts cannot
grant authority. After an authorized, proved pilot, retire legacy components
only in explicit, recoverable batches; never use wildcard/prefix teardown or
SQL cascade.
