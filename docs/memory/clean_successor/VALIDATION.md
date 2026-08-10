# Governed Memory Phase 5 disposable validation contract

## Current proof state

The full Phase 5 disposable harness passed under the preliminary-metadata mode
against candidate HEAD
`699c80761065d19832d2c0f3b2b50342a5a8350c`, tree
`c5c13579ffa54da4f30fc198254c98b662c7029b`, and pre-promotion migration
manifest SHA-256
`2174711255ba55eeb2233703a0e3813a7d9e191b275cadc5959f7aaee3ab9b45`.
Invocation `ac6e240b-1b9e-4442-a592-2b4d1c2e8492` used PostgreSQL 16.14 and
Qdrant 1.19.0. It emitted connect-trace SHA-256
`b054ede64b1f16ce694d110e5d69e4635db41d0f6f2348102d880df8f54338c3`,
foundation dump SHA-256
`852c37925e3a4fc424d4f06456d053bafff3ba8461261303da66b312111c6923`,
bridge dump SHA-256
`c4f802917f69244d6d27cf9bd9e55947e312f22aa9e34fcffe52d4486f09539d`,
and integration receipt SHA-256
`6479f3f0feb8f155754edd3467b8c80f043ae47a19f11ddc459f5c3896fe4c37`.
All invocation-owned resources were removed and proof ports were released.

The exact source-bound CPython 3.12.3 runtime is built and its checked receipt
binds source SHA-256
`af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b`
and wheel SHA-256
`58146af4097400097b1312011c591d1878904f7ac5709b0fdecd57da3fc0f8e4`.

The proof-metadata promotion changes the Git tree, so the attested
pre-promotion HEAD/tree remain immutable receipt facts rather than being
rewritten as the promoted tree. A default-mode reproduction emits a new
`SUCCESSOR_DISPOSABLE_RECEIPT=` JSON line from
`tools/governed_memory_validation/run_disposable_successor.sh`. The receipt is
not added to the tree it attests because doing so would change the tree hash.
Neither receipt extends to production.

Reproduce on the seebx backend only, from the sealed candidate commit:

```bash
GM_VALIDATION_DISPOSABLE_AUTHORIZATION='019fe927:SUCCESSOR_DISPOSABLE_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS' \
GM_VALIDATION_RUNTIME_PYTHON='/tmp/governed-memory-phase5-runtime-94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365-af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b/bin/python' \
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

## Phase 5 proof coverage

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
- explicit activation blockers for the still-unwired worker repository,
  transport, configuration, CLI composition, and cross-process singleton;
- all owner HTTP lifecycle routes and alternating-owner isolation; and
- final resource absence plus unchanged HEAD/tree at completion.

Strict provider and 3072 embedding adapters are currently fake-tested only.
The disposable run must continue to make zero real provider calls unless a
separate scoped authorization explicitly changes that boundary.

Calibration is independently bound by expected artifact and approval-receipt
SHA-256 values. The checked-in artifact remains unapproved; retrieval must stay
off throughout this run.

Frontend candidate `35a684` is built but undeployed. Disposable backend proof
does not substitute for authenticated visual QA.

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
