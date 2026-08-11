# Governed Memory Phase 6E activation boundary

## Current state

Phase 6E is inactive and not authorized. A source-bound candidate runtime has
been built, but its disposable proof has not yet been run or promoted. No
production successor database, Qdrant collection, service, timer, listener,
route, firewall rule, capture membership, erasure-requester membership,
provider request, or pilot marker row has been created. The release guard must
continue to refuse creation with
`activation_blockers_open` and cleanup with `authorization_missing`.

The Phase 6B proof and runtime receipts are historical and noncurrent for this
candidate. A passing Phase 6E disposable deletion proof, separate proof-metadata
promotion, and explicit production authorization are mandatory before
activation review.

## Exact future targets

On the seebx backend, any later authorized pilot remains bounded to:

- HTTP candidate `172.31.32.171:8091`, with Verbal Sage
  `172.31.43.160/32` as the intended source;
- new canonical PostgreSQL at `127.0.0.1:55432`, database
  `governed_memory`;
- new derived Qdrant at `127.0.0.1:6343`, collection
  `governed_memory_9a54cf123493_000001`, alias
  `governed_memory_active`; and
- the existing conversation database through SECURITY DEFINER RPCs only.

Existing databases, collections, volumes, snapshots, claims, vectors, reviews,
preferences, attachment content, and compatibility state cannot seed the
successor.

## Deletion boundary

The inactive candidate permits only exact chat selectors: `thread`,
`message_tail`, `recent`, and `all_conversations`. A future route may coordinate
deletion of exact chat rows, matching chat attachments, matching bridge rows,
eligible empty chat threads, and successor memory derived from those exact
targets. Chat-derived trusted-web response transcripts and an active-thread UI
selection may disappear only through exact named, validated `ON DELETE
CASCADE` composite lineage. Each trusted-web transcript relationship must bind
its chat-log ID together with `owner_user_id` and `thread_id` to
`public.chat_log(id, owner_user_id, thread_id)`. The current live single-column
transcript foreign keys are refused. Every other foreign-key edge, delete
trigger, delete rule, or inheritance edge is a hard refusal.

Activation must preserve exact message/thread targets through the final
acknowledgment and permanent immutable global UUID tombstones afterward. It
must not depend on the legacy capture trigger or function; absence is valid,
while any surviving copy must be exact and disabled.

No future activation may expose a memory-only or account-wide arbitrary Memory
purge. Accounts and structured LifeSwitch data—including libraries, workouts,
weightlifting sessions, food logs, and measurements—must remain outside the
coordinator. Content-free consent, security, audit, and absence receipts must
remain available.

## Activation blockers

The machine-readable runtime manifest is authoritative. At minimum:

1. Complete Phase 6E disposable deletion and full regression proof against a
   clean, sealed candidate; review and promote its metadata separately.
2. Obtain explicit production activation and exact pilot-owner/scope authority.
3. Install and live-verify Supabase user/session authority and credentials.
4. Keep retrieval off until calibration is independently approved.
5. Authorize any real provider or embedding validation separately.
6. Implement sequence-safe Qdrant reconciliation and repair.
7. Authorize and create fresh isolated persistent stores; never adopt old
   targets.
8. Prove port-8091 source firewall and private transport/TLS boundaries.
9. Install HTTP and worker units dormant, then prove exact source,
   configuration, disabled state, and zero listeners.
10. Add and prove a narrowly scoped authenticated chat-deletion route; no route
    is mounted in Phase 6E.
11. Deploy and visually validate the authenticated frontend.
12. Prove legacy owner-scoped read, write, and shadow paths quiescent.
13. Separately authorize and install the exact trusted-web transcript composite
    owner/thread lineage. The nine live transcript rows passed aggregate
    consistency checks, but their current constraint shape is unsafe.
14. Resolve the 2,106 legacy chat rows through an explicit chat-only mapping,
    quarantine, or reset plan. The inventory includes 710 NULL thread IDs and
    118 NULL owners; the counts may overlap. Never include accounts or
    structured LifeSwitch data in that remediation.
15. Retire the complete legacy project-memory branch that currently restricts
    thread deletion, then re-capture the live chat deletion graph. Do not merely
    drop its foreign keys and do not let the coordinator delete project rows.

## Authorized sequence

Each mutation remains a separate approval checkpoint: capture exact pre-state;
seal and prove the runtime; run Phase 6E disposable validation; review proof;
authorize persistent targets and credentials; create empty stores; install
dormant units; prove network and identity boundaries; mount only approved
routes; visually validate; quiesce legacy paths; then authorize one bounded
pilot.

Cleanup remains hard-refused without a separate scoped authorization. Never use
wildcard or prefix teardown, SQL `CASCADE`, or caller-supplied counts as
authority. Legacy retirement occurs only in explicit recoverable batches after
the successor pilot is proved.
