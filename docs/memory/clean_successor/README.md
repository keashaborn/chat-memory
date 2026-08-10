# Governed Memory clean successor — Phase 2 candidate

This is the single current governed-Memory candidate. It is uninstalled and not
production activated. Phase 2 validates it on fresh, empty, disposable
PostgreSQL and Qdrant infrastructure on the seebx backend; it does not apply the
schema to production, read production application rows, call an external
provider, change a service, or change the frontend.

The current implementation is limited to these roots:

```text
governed-memory-migrations/
rag_engine/governed_memory/
tests/memory/
tests/memory_integration/
tools/governed_memory_phase2/
ops/governed_memory/runtime_manifest.json
```

The successor requires Python 3.11 or newer and is validated with Python 3.12.
PostgreSQL is canonical. Qdrant is a disposable derived index and can be rebuilt
only from current PostgreSQL claims, revisions, and applied projection receipts.
Supabase accounts remain the account authority and are not copied or reset.
Legacy claims, cards, vectors, jobs, reviews, preferences, compatibility state,
and attachments are not imported. The attachment interface remains required,
but attachment content is not eligible for release-1 Memory ingestion.

## One current path

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
artifact, old vector, or old Memory row is an input. Owner identity is accepted
only through the verified backend boundary. Release 1 has no administrator or
automatic admission path.

Selected-span SHA-256 and UTF-8 offsets are distinct from the whole-message
hash. One checked-in predicate catalog governs extraction, review, correction,
admission, projection, and retrieval. A provider call must be durably marked
dispatched before invocation; an unknown post-dispatch outcome is terminal and
cannot authorize an automatic second call.

Qdrant returns bounded candidate identifiers and hashes only. It cannot supply
owner identity, fact content, policy, lifecycle, current revision, or rebuild
truth. PostgreSQL reloads the candidate claim and revision, recomputes their
hashes, and enforces retrieval policy before model-visible rendering.

The model-visible record contains only predicate, subject display, object, and
epistemic state. Answer binding has two terminal outcomes: `exposed` and
`no_memory_selected`. It persists content-free hashes, identifiers, policy
metadata, and offsets for 90 days. Pending proposals expire. Terminal proposal replay
is bounded to 30 days. After that window, exact replay is unavailable and
returns `proposal_retention_purged` from content-free audit evidence. Verified
claim-deletion receipts and audit records remain content-free after claim
content is purged.

## Phase 2 validation

The fail-closed harness proves the following against disposable PostgreSQL and
Qdrant, followed by exact resource removal:

- migration forward, empty-only rollback, absence, reapply, and normalized
  catalog equivalence;
- forced RLS, cross-owner denial, exact grants, and direct table-DML denial;
- pre-dispatch retry and post-dispatch single-call behavior with zero external
  provider calls;
- a synthetic Chat A through review, claim, projection, Qdrant retrieval,
  PostgreSQL revalidation, and a distinct Chat B answer binding;
- cold extraction-worker reconstruction after bridge purge;
- correction, retention purge, cold PostgreSQL-driven vector rebuild, alias
  swap, retraction, verified Qdrant deletion, and structured hard deletion;
- `production_data_read=false` and removal of every run-owned container,
  volume, and network.

The integration uses synthetic database identities, a synthetic provider
completion, and deterministic vectors. It does not prove Supabase-to-database
identity propagation, installed HTTP routes, real provider/embedding adapters,
or authenticated frontend behavior.

## Activation and retirement boundary

Production remains unchanged. A new empty PostgreSQL database and new empty
versioned Qdrant collection should be used at an authorized cutover; neither
store should be prefilled with old or unprocessed Memory data.

Production activation is blocked until an authorized holdout calibrates and
binds a minimum semantic retrieval-score threshold. It is also blocked until
Supabase owner propagation, service routes, real adapters, and authenticated
frontend behavior are integration-tested.

Old services, timers, SQL objects, vectors, source, tests, branches, worktrees,
and compatibility routes are not deleted by this candidate. After activation,
retire them in bounded waves: prove no caller or nonterminal work, revoke or
disable first, observe, preserve one immutable recovery manifest, then delete
only the exact approved batch with a receipt.
