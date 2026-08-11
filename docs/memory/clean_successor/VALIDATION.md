# Governed Memory Phase 6B disposable validation contract

## Current proof state

The full Phase 6B disposable harness passed under the explicitly authorized
preliminary-metadata mode against candidate HEAD
`7693d9db459f81f4d89e680108867ce31dd4c7ed`, tree
`f62ad8e9cccffe715927fa825f24cb4312a27934`, and pre-promotion migration
manifest SHA-256
`3bfe6ce5f2514f642dee58416d12f0bfa938646897b70b3e3294f8e1639b2c66`.
Invocation `1547b8b5-e238-4817-af95-66b565633704` used PostgreSQL 16.14 and
Qdrant 1.19.0. It emitted connect-trace SHA-256
`39328c4fcbd6f2bd18ba40a0cd3aa14daf179d0d8e62597f39d932c91a396956`,
foundation dump SHA-256
`141cf9164a5edc0dfbbc254a9ea07f81116933959e45173af02600a8c7083404`,
bridge dump SHA-256
`fd13d38ed3f56312969262fa27d9e706df6ee28222c6d28b4b0036602f025949`,
and integration receipt SHA-256
`f6644fe1ac106117195885e46cd97e6436bbb772a4a1b2a2a2490a9101b28096`.
All exact invocation-owned resources were removed and proof ports were
released. The complete terminal v5 receipt is preserved in
`ops/governed_memory/phase6b_disposable_proof_receipt.json`.

The current Phase 6B source-bound CPython 3.12.3 runtime receipt at
`ops/governed_memory/runtime_build_receipt.json` has SHA-256
`ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69`.
It binds installable successor source-inventory SHA-256
`d08cc71966beec1e31e107c08b71daa4e51daf3c0b3b6f5ef584ef8bae41c0e0`
and wheel SHA-256
`c1605f2a572dfde4d1c5b6246d331a88413f3db051cbb8a3c24ffdf6be98c5db`.
Its isolated import sweep covers all installed successor modules and rejects
outer `rag_engine` modules, the OpenAI SDK, and files outside the runtime.
That runtime was used by the passing Phase 6B disposable migration and
two-database worker run.

The authorized proof-metadata promotion changes the Git tree, so the attested
pre-promotion HEAD/tree remain immutable receipt facts rather than being
rewritten as the promoted tree. Default-mode reproduction omits the preliminary
authorization and emits a new `SUCCESSOR_DISPOSABLE_RECEIPT=` JSON line from
`tools/governed_memory_validation/run_disposable_successor.sh`. A reproduction
receipt is not added to the same tree it attests because doing so would change
the tree hash. Neither mode extends to production.

Reproduce on the seebx backend only, from the sealed candidate commit:

```bash
GM_VALIDATION_DISPOSABLE_AUTHORIZATION='019fe927:SUCCESSOR_DISPOSABLE_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS' \
GM_VALIDATION_RUNTIME_PYTHON='<phase6b-candidate-python>' \
GM_VALIDATION_EXPECTED_ROOT='<absolute-candidate-worktree>' \
GM_VALIDATION_EXPECTED_BRANCH='<candidate-branch>' \
GM_VALIDATION_EXPECTED_HEAD='<candidate-head>' \
GM_VALIDATION_EXPECTED_TREE='<candidate-tree>' \
bash tools/governed_memory_validation/run_disposable_successor.sh full
```

## Required bindings

The run must bind one unchanged candidate to:

- exact Git HEAD/tree and migration manifest;
- exact package source-tree, runtime/build locks, wheel, installed files, and
  final runtime receipt;
- fresh empty disposable PostgreSQL and Qdrant targets;
- PostgreSQL 16 pinned digest and Qdrant v1.19.0 pinned digest
  `057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`;
- no production data, no production endpoint, and zero external provider calls;
  and
- cleanup of every invocation-owned container, volume, network, relay, and
  process.

## Phase 6B proof coverage

The passing run proved:

- migrations 0001 through 0004 in manifest order, rollback/reapply where
  authorized, normalized catalog equivalence, exact grants, forced RLS, and
  direct-DML denial;
- the owner claim-detail surface and content bounds;
- mandatory signed `session_id` resolution through the staged Supabase
  `auth.sessions` RPC, including absent, revoked, expired, outage, wrong-owner,
  and malformed results;
- the content-free monotonic pilot marker: zero-row absence, exact first insert,
  exact replay, conflicting replay refusal, and rollback refusal after start;
- Qdrant v1.19.0 compatibility for exact alias/physical target, size 3072,
  `Dot`, six required indexes, bounded owner search without vectors, ambiguous
  upsert readback, and alias-plus-physical deletion verification;
- inactive real two-database worker repository, transport, configuration, CLI
  composition, persistent fairness cursor, and cross-process singleton;
- all owner HTTP lifecycle routes and alternating-owner isolation; and
- final resource absence plus unchanged HEAD/tree at completion.

Strict provider and 3072 embedding adapters are currently fake-tested only.
The embedding path now requires a durable content-free request marker before
HTTP dispatch and terminalizes marked lease expiry or unresolved post-dispatch
Qdrant failure rather than resending the embedding request.
The disposable run must continue to make zero real provider calls unless a
separate scoped authorization explicitly changes that boundary.

Calibration is independently bound by expected artifact and approval-receipt
SHA-256 values. The checked-in artifact remains unapproved; retrieval must stay
off throughout this run.

Frontend candidate `6d80ba` is built but undeployed. Disposable backend proof
does not substitute for authenticated visual QA.

## Remaining validation gaps

The passing run did not exercise the rolling 20-row capture ceiling to its
boundary, call a real provider or embedding endpoint, verify live Supabase
session freshness, approve semantic calibration, prove persistent-store
operation, install production routes, or validate the authenticated frontend.
Chat-deletion cancellation/erasure coordination is not implemented and remains
an activation blocker.

## Corrections made before the passing run

- The pilot-marker `ON CONFLICT` output-variable ambiguity was removed with a
  named primary-key constraint and `ON CONFLICT ON CONSTRAINT`.
- Stale validation expectations were updated to the exact 14-table schema and
  forced-RLS inventory before the successful run.
- The invalid Qdrant alias endpoint was replaced with the exact collection
  alias endpoint and verified against disposable Qdrant 1.19.0 only. This is
  not persistent-pilot approval.

## What a passing receipt does not authorize

Even a passing disposable receipt is not production activation proof. It does
not install services, create persistent targets, authorize a pilot owner, call a
real provider, deploy the frontend, approve calibration, prove production
firewall/TLS, or quiesce legacy Memory paths.
