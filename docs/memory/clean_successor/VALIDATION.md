# Governed Memory Phase 6E disposable validation contract

## Current state

Phase 6E is a sealed inactive candidate with a source-bound runtime build. Its
current receipt is `ops/governed_memory/runtime_build_receipt.json`; the Phase
6B runtime receipt is preserved at
`ops/governed_memory/history/phase6b/runtime_build_receipt.json`. The current
candidate has not yet received a passing disposable PostgreSQL and Qdrant proof
receipt.

Do not describe Phase 6B results as proof of Phase 6E deletion coordination.
Do not run the harness against production stores or with production endpoints.
The checked Phase 6E harness implements the required cases below but remains
fail-closed unless the invocation supplies the exact proof authorization,
sealed Git HEAD/tree, expected source-bound runtime, and fresh disposable
resource identities. Metadata remains unpromoted until the emitted proof is
reviewed separately.

## Phase 6E required bindings

The run must bind one clean, sealed candidate to:

- exact Git HEAD/tree and migration manifest;
- exact source inventory, runtime/build locks, wheel, installed files, and new
  runtime receipt;
- fresh empty disposable PostgreSQL and Qdrant targets;
- no production data, endpoint, service, membership, or persistent mount;
- zero external provider and embedding calls; and
- cleanup of every invocation-owned resource.

The migration verifier must run in preliminary proof mode while package and
root status remain not-yet-disposable-validated. Metadata promotion is a later
checkpoint after the emitted receipt is reviewed.

## Required chat-erasure cases

The Phase 6E disposable run must prove:

1. Only `thread`, `message_tail`, `recent`, and `all_conversations` selectors
   are accepted.
2. The conversation database materializes an exact owner-scoped target count,
   selector hash, per-target hash, and ordered manifest before deletion.
3. Matching pending, claimed, or retryable bridge work is fenced or cancelled;
   no stale extraction or projection work can resurrect targeted content.
4. Only successor evidence, jobs, proposals, claims, answer bindings, and
   vectors derived from exact targeted chat rows are removed.
5. Every touched claim uses the existing verified Qdrant deletion receipt
   lifecycle; the coordinator does not perform an unverified bulk vector purge.
6. Matching `public.chat_attachments` and `public.chat_log` rows are deleted,
   only eligible empty `public.threads` rows are removed, and the exact
   chat-derived trusted-web transcript and active-thread selection effects are
   absence-verified.
7. Accounts, libraries, workouts and weightlifting sessions, daily food logs,
   measurements, and all other structured LifeSwitch tables are unchanged.
8. Memory-only and account-wide arbitrary Memory selectors are absent and
   rejected.
9. Content-free consent, security, audit, governed-absence, conversation, and
   final receipts remain.
10. Exact replay, conflicting replay, lease expiry, retryable failure, manual
    review, crash recovery at every durable boundary, owner isolation, and
    final absence are deterministic.
11. Partial target-page replay, future-dated chat rows, attachment movement,
    exhausted attempts, and stale owner/authentication context fail closed.
12. Migration-time and transaction-local runtime catalog checks reject every
    foreign-key edge, delete trigger, delete rule, or inheritance edge outside
    the closed chat graph. The weak live single-column trusted-web transcript
    foreign keys must be refused. A clean fixture must instead use exact named,
    validated `ON DELETE CASCADE` composite lineage from
    `(user_chat_log_id, owner_user_id, thread_id)` and
    `(assistant_chat_log_id, owner_user_id, thread_id)` to
    `public.chat_log(id, owner_user_id, thread_id)`.
13. Exact message and thread target inventories survive through final
    acknowledgment; permanent immutable global UUID tombstones block replay,
    and validation passes when the disabled legacy capture trigger is fully
    absent while refusing any enabled or drifted copy.

The current production catalog has two legacy project-memory foreign keys into
`public.threads`. Phase 6E must prove that the clean bridge refuses that mixed
catalog. A separate disposable fixture with only the permitted chat graph must
prove deletion. Production separation of the complete legacy project branch is
not part of Phase 6E and is not authorized by this validation contract.

The read-only live seebx audit found nine trusted-web transcript rows. Their
owner/thread aggregates were consistent with both referenced chat rows, but
the installed constraints remain unsafe single-column foreign keys. Correcting
that production schema requires separate authorization; Phase 6E proves only
refusal of the weak shape and behavior of the clean composite fixture.

The audit also found 2,106 `public.chat_log` rows, including 710 with a NULL
`thread_id` and 118 with a NULL owner; the two counts may overlap. Production
mapping, quarantine, or reset of those rows is a separate chat-only decision
before activation. It must not select, mutate, or delete accounts or any
structured LifeSwitch table.

## Existing successor coverage to rerun

Because the candidate changes foundation and bridge migrations plus worker source,
Phase 6E must rerun migration forward/rollback/reapply, normalized catalog
equivalence, exact grants, forced RLS, direct-DML denial, owner HTTP lifecycle,
alternating-owner isolation, cold extraction and projection rebuild, claim
correction/retraction/hard deletion, pilot-marker behavior, two-database worker
composition, fairness, and cross-process singleton refusal.

The harness must still exclude attachment content from Memory ingestion and
must keep semantic retrieval off. Real provider or embedding calls require a
separate explicit authorization and are outside Phase 6E by default.

## A passing receipt does not authorize activation

A disposable receipt does not install services, create persistent targets,
grant production memberships, mount a deletion route, authorize a pilot owner,
deploy the frontend, approve calibration, prove firewall/TLS, quiesce legacy
paths, or delete legacy components. Those remain separate checkpoints.
