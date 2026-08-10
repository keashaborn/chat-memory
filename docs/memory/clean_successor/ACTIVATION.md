# Governed Memory Phase 6B activation boundary

## Current state

The Phase 6B source candidate is inactive and not authorized. Neither systemd
template is installed or enabled. No production successor database, collection,
listener, route, firewall rule, capture membership, provider request, or pilot
marker row exists. The release guard returns `activation_blockers_open` for
create and `authorization_missing` for cleanup, with an empty action plan and
zero executed commands.

The current checked Phase 6B runtime receipt has SHA-256
`ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69`
and binds installable successor source-inventory SHA-256
`d08cc71966beec1e31e107c08b71daa4e51daf3c0b3b6f5ef584ef8bae41c0e0`
to the sealed CPython 3.12.3 candidate. The full Phase 6B disposable migration
and two-database worker receipt remain pending. The runtime receipt alone is
not production activation evidence.

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
- Owner-scoped claim detail is implemented and candidate-tested; its older
  proof is historical and fresh Phase 6B disposable revalidation is pending.
- Strict provider and 3072 embedding adapters are fake-tested. Embedding HTTP
  dispatch requires a durable content-free request marker; marked requests are
  never automatically resent. The exact
  Qdrant adapter is fake-tested and proved against disposable Qdrant 1.19.0;
  no real provider call has occurred.
- Migration 0004 implements a content-free append-only pilot marker; its older
  proof is historical and fresh Phase 6B disposable revalidation is pending.
- The one-item worker has inactive exact two-database composition, RPC-only
  repositories, loopback Qdrant transport, exact target configuration,
  persistent three-lane fairness, and PostgreSQL session advisory locking.
  These are focused-test results only; fresh disposable and concurrent proof
  remain open.
- Calibration requires independent artifact and approval-receipt hashes. Its
  artifact is unapproved and retrieval remains off.
- Frontend candidate `6d80ba` is built, undeployed, and awaiting authenticated
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
5. Use the fresh sealed runtime to prove the Phase 6B two-database worker,
   persistent fairness cursor, attachment exclusion, exact pilot identity, and
   cross-process singleton against fresh disposable stores.
6. Authorize any real provider/embedding validation separately; unknown
   post-dispatch outcomes remain terminal and are never automatically retried.
   Implement and authorize projection reconciliation with a sequence-safe
   Qdrant repair fence; unresolved marked work blocks later same-claim
   projection until then.
7. Create fresh isolated persistent PostgreSQL and Qdrant targets only after a
   scoped creation authorization and implement the production store-role
   bootstrap.
8. Prove port-8091 source firewall and private transport/TLS boundaries.
9. Install the HTTP and worker units in mode `off`, then prove their exact
    source, configuration, disabled state, and zero listeners.
10. Authorize and activate capture for one exact owner only.
11. Deploy frontend candidate `6d80ba` and complete authenticated visual QA.
12. Prove owner-scoped legacy Memory read/write/shadow paths are quiesced.
13. Authorize one owner, at most 20 post-cutover messages, at most 24 hours,
    and one generation call per exact attempt.
14. Implement and prove chat-deletion cancellation/erasure coordination.

## Authorized sequence

Each step is a separate checkpoint:

1. Capture seebx hostname, HEAD/tree, services, ports, store identities, and
   legacy flags in a content-free pre-state receipt.
2. Verify and use the checked runtime receipt that binds the sealed Phase 6B
   source, runtime/build locks, installed package, and wheel bytes. Rebuild it
   if any successor package byte or lock changes; never reuse the Phase 5
   receipt.
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
