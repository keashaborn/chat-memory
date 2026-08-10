# Governed Memory clean successor — current candidate

This is the single current governed-Memory candidate. It is uninstalled and
not production activated. Validation runs only against fresh, empty,
disposable PostgreSQL and Qdrant stores on an internal Docker network plus
invocation-owned loopback JWKS and HTTP processes on the seebx backend. It does
not apply schema to production, read production application rows, call a
provider, change a service, or change the frontend.

The current implementation is limited to these roots:

```text
governed-memory-migrations/
rag_engine/governed_memory/
tests/memory/
tests/memory_integration/
tools/governed_memory_validation/
ops/governed_memory/runtime_manifest.json
```

The successor requires Python 3.11 or newer. Disposable validation uses Python
3.12.3 and the exact installed package versions recorded in
`tools/governed_memory_validation/runtime_packages.json`. That artifact records
the environment actually exercised; it is not a candidate-built install lock.
PostgreSQL is canonical. Qdrant is a derived index rebuilt only from current
PostgreSQL claims, revisions, and applied projection receipts. Supabase remains
the account authority; accounts are neither copied nor reset. Legacy claims,
cards, vectors, jobs, reviews, preferences, compatibility state, and attachment
content are not imported. The attachment interface remains required, but
attachment content is not eligible for release-1 Memory ingestion.

## One current path

```text
post-cutover user message
  -> content-free conversation bridge
  -> five-outcome deterministic eligibility
  -> one durable extraction attempt
  -> explicit owner review over authenticated HTTP
  -> PostgreSQL claim and immutable revision
  -> projection outbox
  -> derived Qdrant candidate
  -> PostgreSQL revalidation
  -> answer binding
```

No legacy table, route, worker, versioned Python module, filesystem review
artifact, old vector, or old Memory row is an input. Release 1 has no
administrator or automatic admission path.

The owner HTTP boundary accepts an infrastructure service token and an
asymmetrically signed Supabase-style user access token. It derives owner and
actor only from the verified UUID `sub`; caller headers, paths, queries, bodies,
`user_metadata`, and `app_metadata` cannot set authority. Each request binds the
verified owner with `SET LOCAL` inside one short PostgreSQL transaction. Forced
RLS and owner-scoped stored procedures remain the database authority.

Selected-span SHA-256 and UTF-8 offsets are distinct from the whole-message
hash. One checked-in predicate catalog governs extraction, review, correction,
admission, projection, and retrieval. A provider call must be durably marked
dispatched before invocation; an unknown post-dispatch outcome is terminal and
cannot authorize an automatic second call.

Qdrant returns bounded candidate identifiers and hashes only. It cannot supply
owner identity, fact content, policy, lifecycle, current revision, or rebuild
truth. PostgreSQL reloads candidate claims and revisions, recomputes their
hashes, and enforces retrieval policy before model-visible rendering.

The model-visible record contains only predicate, subject display, object, and
epistemic state. Answer binding has two terminal outcomes: `exposed` and
`no_memory_selected`. It persists content-free hashes, identifiers, policy
metadata, and offsets for 90 days. Pending proposals expire.
Terminal proposal replay is bounded to 30 days; after that, exact replay is
unavailable and
returns `proposal_retention_purged` from content-free audit evidence. Verified
claim-deletion receipts and audit records remain content-free after claim
content is purged.

## Disposable validation

The fail-closed harness proves the following against disposable resources and
then removes those exact resources:

- migration forward, empty-only rollback, absence, reapply, and normalized
  catalog equivalence;
- forced RLS, cross-owner denial, exact grants, and direct table-DML denial;
- locally signed ES256 tokens resolved through a bounded loopback JWKS cache,
  exact issuer/audience checks, forged-metadata denial, and a negative JWT
  matrix;
- owner A and owner B alternating through one connection pool fixed at
  `max_size=1`, without owner-context leakage;
- all nine owner HTTP routes: status, claim and proposal lists, claim read,
  proposal review, correction, retraction, deletion, and operation read;
- pre-dispatch retry and post-dispatch single-call behavior with zero external
  provider calls;
- synthetic Chat A through review, claim, projection, Qdrant retrieval,
  PostgreSQL revalidation, and a distinct Chat B answer binding;
- cold worker reconstruction, correction, retention purge, PostgreSQL-driven
  vector rebuild, alias swap, retraction, verified Qdrant deletion, and
  structured hard deletion; and
- no published container ports: the integration process owns invocation-local
  loopback relays, and the connection trace admits only the four declared
  loopback endpoints plus the exact two internal container endpoints; and
- `production_data_read=false`, `production_endpoint_calls=0`, and removal of
  every invocation-owned container, network, relay, and process.

This proves the candidate boundary with synthetic identities. It does not prove
live Supabase session freshness/revocation, a decided durable policy for
session/key-bound authentication provenance, a candidate-owned runtime
environment, production route mounting, authenticated frontend behavior, real
provider/embedding adapters, or a calibrated semantic retrieval-score
threshold.

## Activation and retirement boundary

Production remains unchanged. Authorized cutover should use a new empty
PostgreSQL database and a new empty Qdrant collection; neither should be
prefilled with old or unprocessed Memory data.

Activation remains blocked on semantic-threshold calibration, live Supabase
signing-key and session-freshness wiring, a durable authentication-provenance
policy, a candidate-owned runtime environment, production service mounting,
real provider/embedding adapters, and authenticated frontend validation.

Old services, timers, SQL objects, vectors, source, tests, branches, worktrees,
and compatibility routes are not deleted by this candidate. After activation,
retire them in bounded waves: prove no caller or nonterminal work, revoke or
disable first, observe, preserve one immutable recovery manifest, then delete
only the exact approved batch with a receipt.
