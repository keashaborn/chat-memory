# Governed Memory clean successor — Phase 1 candidate

Phase 1 is an offline, uninstalled candidate. It defines the fresh PostgreSQL
contract, a deterministic versionless Python core, synthetic fixtures, and the
twelve discovered successor test modules. It does not create a database or
Qdrant collection, call a provider, install a service, change the frontend, read
application rows, migrate historical data, or activate Memory.

The implementation is rooted at:

```text
governed-memory-migrations/
rag_engine/governed_memory/
tests/memory/
ops/governed_memory/runtime_manifest.json
```

The new `governed_memory` database is authoritative. The conversation database
bridge is content-free. Qdrant remains an empty, derived future integration and
is not contacted in this phase. Attachment-content ingestion is rejected for
release 1. Existing accounts remain authoritative in Supabase; no accounts or
historical Memory state are copied.

The successor core requires Python 3.11 or newer; candidate validation uses
Python 3.12. The system Python 3.9 on the Local Mac is not a supported test
runtime.

## Release 1 path

The only proposed active path is:

```text
post-cutover user message
  -> content-free conversation bridge
  -> five-outcome deterministic eligibility
  -> one durable extraction attempt
  -> explicit owner review
  -> PostgreSQL claim and immutable revision
  -> projection outbox
  -> derived Qdrant candidate
  -> PostgreSQL revalidation
  -> answer binding
```

No legacy table, route, worker, versioned Python module, filesystem review
artifact, existing Qdrant point, or existing Memory row is an input to this
path. The owner is derived from verified authentication. Release 1 has no
administrator admission path and no automatic admission path.

Exact selected-span SHA-256 and UTF-8 offsets are distinct from the whole
conversation-message hash. The predicate catalog is one injected, checked-in
contract shared by extraction, review, correction, PostgreSQL admission, and
projection. A provider call is durably marked dispatched before invocation. If
that dispatched outcome becomes unknowable after a crash, the job terminates as
`outcome_unknown` and is not called again automatically.

Qdrant may propose bounded claim identifiers only. It is never authority for
owner, fact content, current revision, lifecycle, predicate policy, validity,
or retrieval eligibility. Before any fact is rendered, PostgreSQL must
revalidate the current claim and revision, recompute their hashes and state,
and enforce the typed retrieval-policy envelope. Phase 1 does not yet prove
that a live Qdrant candidate actually came from the successor collection; that
trusted adapter boundary is a Phase 2 integration gate.

The model-visible record contains only predicate, subject display, object, and
epistemic state. Answer binding has exactly two terminal outcomes: `exposed`
and `no_memory_selected`. It uses the fixed release-1 renderer limits of eight
records and 32,768 UTF-8 bytes; a smaller policy maximum may further limit
selection. There is no selected-but-not-injected state or caller-chosen failure
code. Bindings retain only content-free hashes, identifiers, policy metadata,
and byte offsets. They expire exactly 90 days after one server-owned creation
timestamp and are removed only by the bounded worker purge, which emits a
content-free audit receipt.

Pending proposals are either explicitly reviewed or expire.
Terminal proposal replay is intentionally bounded by the terminal-row retention
window, with a maximum of 30 days. After purge, exact replay is unavailable and returns
`proposal_retention_purged`; the retained signal is content-free. An old
operation identifier is therefore not a permanent replay oracle.
Claim-deletion receipts and content-free audit records are not part of that
purge.

## Phase 2 gates

Phase 2 must use disposable, empty PostgreSQL and Qdrant instances and prove:

- forward, rollback, and reapply migrations against the real PostgreSQL
  catalog;
- forced RLS, owner isolation, exact function ownership and grants, and denial
  of direct runtime-role table DML;
- crash/replay behavior before and after provider dispatch;
- one synthetic Chat A through review, claim, projection, Qdrant candidate,
  PostgreSQL revalidation, and separate Chat B exposure;
- correction, retraction, hard deletion, vector rebuild, and retention purges;
- exact source binding for the content-free conversation bridge; and
- empty successor stores with no legacy import or historical backfill.

Old Memory services, timers, SQL objects, vectors, source, tests, branches,
worktrees, and compatibility routes are not retired or deleted by Phase 1.
They remain separately classified until successor cutover, caller observation,
an immutable retention manifest, rollback proof, and an explicitly authorized
deletion batch.

Phase 1 validation is deliberately limited to deterministic unit tests and
independent static inspection of SQL/runtime artifacts. It is not database,
RLS-catalog, Qdrant, provider, service, frontend, or end-to-end production
proof. Those require disposable integration infrastructure in Phase 2.
