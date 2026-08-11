# Governed Memory clean successor — Phase 6D inactive candidate

This repository contains the single current governed-Memory successor source
candidate. It is not production activated: no successor service, listener,
route, timer, capture membership, PostgreSQL database, or Qdrant collection has
been installed, enabled, started, or created. No provider call is authorized.

PostgreSQL remains canonical and Qdrant remains derived and rebuildable.
Supabase remains account authority; accounts are not copied into Memory. Legacy
claims, cards, vectors, jobs, reviews, preferences, compatibility state, and
attachment content are not imported.

The successor requires CPython 3.12.x. A new source-bound CPython 3.12.3
runtime build is pending. Phase 6D made zero external provider calls, and no
semantic retrieval-score threshold is activated.

## Phase 6D addition: chat-source erasure

The inactive deletion coordinator is a fenced, replay-safe two-database saga.
It accepts only these conversation selectors:

- `thread`
- `message_tail`
- `recent`
- `all_conversations`

Each request first materializes an exact owner-scoped chat target manifest.
Only those chat rows, their matching chat attachments, matching bridge rows,
eligible empty chat threads, and successor memory derived from those exact chat
targets participate. Claim-vector deletion continues through the existing
verified per-claim PostgreSQL/Qdrant lifecycle before chat rows are removed.
The only permitted database-driven side effects are deletion of chat-derived
trusted-web response transcripts and the active-thread UI selection attached
to a deleted thread. Trusted-web transcript effects require exact named,
validated `ON DELETE CASCADE` composite lineage from
`(user_chat_log_id, owner_user_id, thread_id)` and
`(assistant_chat_log_id, owner_user_id, thread_id)` to
`public.chat_log(id, owner_user_id, thread_id)`. Weak single-column transcript
foreign keys are refused. The migration and runtime reject every unclassified
foreign-key edge, delete trigger, delete rule, and inheritance edge.

The bridge retains exact message and thread target inventories until final
acknowledgment. It then keeps only immutable, content-free global UUID
tombstones in `source_erasure_message_tombstone` and
`source_erasure_thread_tombstone`, preventing deleted chat identities from
being recreated. The old capture trigger is not required; if a transitional
copy remains, it must have the exact expected identity and be disabled.

There is no memory-only selector and no account-wide arbitrary Memory purge.
The coordinator cannot delete accounts, LifeSwitch libraries, workouts or
weightlifting sessions, daily food logs, measurements, or any other structured
LifeSwitch tracking data. Content-free consent, security, audit, and final
absence receipts are retained.

The coordinator is source-only and inactive. It has no mounted HTTP route, no
new service or timer, no production role membership, and no authority to run
against production data.

## Current successor path

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

Attachment storage remains available, but attachment content is not Memory
input. A chat-erasure request may delete matching attachment rows only because
they belong to the exact selected chat scope. Thread and all-conversation
selectors also include unattached rows owned by the selected chat threads.

The current mixed production conversation database still has two restrictive
legacy project-memory relationships to `public.threads`. The clean bridge
refuses installation while either relationship exists; it does not delete or
install compatibility hooks on those project records. Their complete dependent
legacy branch must be retired in a separately authorized, receipted cleanup
after legacy writers are quiescent.

The read-only live seebx catalog audit found nine rows in
`trusted_web.response_transcript_v1`. Aggregate checks found their owner and
thread values consistent with both referenced chat rows, but the live foreign
keys protect only `user_chat_log_id` and `assistant_chat_log_id`. That
constraint shape remains unsafe and is refused. Installing the required
composite lineage is a separately authorized schema correction, not part of
this inactive candidate.

The same audit found 2,106 rows in `public.chat_log`, including 710 rows with a
NULL `thread_id` and 118 rows with a NULL owner; those counts may overlap.
Before activation, those chat rows require an explicit chat-only mapping,
quarantine, or reset decision. That remediation must never include accounts,
libraries, workouts or weightlifting sessions, food logs, measurements, or any
other structured LifeSwitch data.

Terminal proposal replay remains bounded to 30 days. After retention removes a
terminal proposal, exact replay is unavailable; `proposal_retention_purged` is
returned from content-free audit evidence without reconstructing deleted
proposal content.

## Proof boundary

The checked Phase 6B runtime-build and disposable proof receipts are preserved
as historical evidence. They attest the earlier Phase 6B source and migration
bytes only. Phase 6D changed runtime and migration source, so neither receipt is
current proof and neither may be reused for activation.

Phase 6E must build a new source-bound runtime and run the full disposable
PostgreSQL/Qdrant harness. It must prove exact target selection, cancellation of
matching ingest work, verified claim/vector deletion, absence checks, chat and
attachment deletion, empty-thread rules, replay, retry, crash recovery,
owner-isolation, closed foreign-key/trigger/rule/inheritance scope,
structured-LifeSwitch preservation, and content-free receipts.
Until that proof passes, all migration packages remain
`isolated_candidate_not_yet_disposable_validated_not_production_applied`.

## Other inactive boundaries

- Signed Supabase JWT `sub` remains owner authority and signed `session_id` is
  mandatory. The `auth.sessions` RPC is staged but not installed or live
  verified.
- Provider and embedding adapters remain fake-tested with zero authorized real
  calls.
- Semantic calibration is unapproved, so retrieval remains off.
- Persistent PostgreSQL and Qdrant targets are not approved or created.
- Frontend candidate `6d80ba` remains built, undeployed, and visually
  unverified.
- Legacy owner-scoped read, write, and shadow paths are not yet proved
  quiescent.

Production remains unchanged. Old services, stores, source, tests, and
compatibility routes are not deleted by Phase 6D. Retirement remains a later,
explicitly authorized sequence with exact targets, recovery evidence, an
observation window, and a content-free deletion receipt.
