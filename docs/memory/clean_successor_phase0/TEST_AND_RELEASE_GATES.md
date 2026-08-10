# Governed Memory clean-successor tests and release gates

## Test policy

Only tests protecting the clean current contract remain in active test discovery. Tests that exclusively protect legacy cards, Vantage, raw Qdrant writes, filesystem review packets, local V5/V5.1/V5.2 routing/staging/resolution, retired systemd units, old manifests, or compatibility cookies are removed with those components after the immutable archive checkpoint.

At backend authority commit `cc12c834d9fc025b0208a06044b6760343fd03e5`, the repository has 494 tracked test artifacts. There are 345 Memory-named test paths and 34 additional committed `evals/memory_v1*` artifacts. The 153 `tests/test_memory*.py` files alone contain 37,585 lines. Current CI runs only four focused Memory tests, three of which pin compatibility workers intended for retirement. This is not a current-system release gate.

The clean backend suite contains exactly these active files:

```text
tests/memory/test_schema_and_rls.py
tests/memory/test_intake_boundary.py
tests/memory/test_eligibility.py
tests/memory/test_extraction.py
tests/memory/test_admission.py
tests/memory/test_projection.py
tests/memory/test_retrieval.py
tests/memory/test_prompt_and_binding.py
tests/memory/test_lifecycle.py
tests/memory/test_worker_recovery.py
tests/memory/test_chat_memory_e2e.py
tests/memory/test_runtime_inventory.py
```

Use three tiers:

1. Fast deterministic tests with no network, provider, production database, or Qdrant access.
2. Integration tests in a disposable PostgreSQL database and disposable Qdrant collection.
3. Release proof using a clean isolated build, synthetic authenticated owner, exact provider-call bound, and explicit rollback.

## Minimum fast tests

- All five eligibility outcomes remain distinct and preserve exchange/window lineage.
- Ineligible input creates no provider reservation, proposal, evidence text, or vector work.
- Idempotency prevents duplicate jobs, proposals, admissions, revisions, and outbox items.
- Provider request envelope and completion receipt hashes bind owner, evidence, job, model, schema, operation, request, and response.
- Invalid provider output cannot enter proposal or claim state.
- Admission requires the exact proposal, evidence hash, reviewer authority, and current pending state.
- Correction immediately suppresses the disputed revision; reviewed replacement creates an immutable revision; retraction/deletion never overwrite history.
- Retrieval excludes noncurrent, retracted, deleted, cross-owner, and hash-mismatched candidates.
- Memory-selection failure degrades to a normal answer; mutation/owner failures fail closed.
- Attachment content cannot enter Memory without an explicit bounded remember action.
- The runtime manifest rejects legacy units, routes, imports, collection aliases, and database APIs.

## Minimum integration tests

- Forward migration, catalog fingerprint, forced RLS, grants, fixed search paths, rollback, reapply, and unchanged fingerprint after reapply.
- Cross-owner SELECT/INSERT/UPDATE/DELETE and function execution denial.
- Bounded concurrent job claim, crash-before-provider, crash-after-provider, retry, and terminal-attempt behavior.
- Proposal review race and admission replay.
- Claim correction/retraction/deletion with exact outbox ordering.
- Qdrant upsert/delete replay and complete rebuild from active PostgreSQL revisions.
- Stale or malicious Qdrant payload cannot survive PostgreSQL revalidation.
- Zero-Memory retrieval performs no provider call and produces no false context.
- Deletion propagation reaches PostgreSQL current state, outbox, Qdrant, answer inspection, and retained receipt without deleting the Supabase account.

## End-to-end release proof

One synthetic authenticated account must prove:

1. Chat A creates one exact eligible job.
2. At most one provider call creates one validated proposal.
3. Explicit review admits one claim revision.
4. One outbox item creates one derived Qdrant point.
5. Separate Chat B retrieves that point, revalidates it in PostgreSQL, and records a bounded answer binding.
6. Correction replaces retrieval with the new revision.
7. Retraction and deletion remove prompt eligibility even if a stale vector remains temporarily.
8. A second owner cannot discover or operate on any first-owner object.
9. Memory unavailability returns a normal non-Memory answer with a content-free receipt.
10. The system rebuilds the Qdrant collection from PostgreSQL and rolls back the application/database candidate without legacy fallback.

## Retirement gates

No legacy component is disabled or removed until:

- the successor is installed inactive and hash-bound;
- the exact end-to-end release proof passes;
- no legacy or historical data is imported;
- all legacy jobs/files are terminally counted;
- frontend and backend callers are proven migrated or absent;
- the production runtime manifest rejects the legacy component;
- an immutable external source/configuration bundle exists;
- rollback does not require restoring legacy Memory data;
- the user separately authorizes the exact runtime, database, Qdrant, file, or Git deletion batch.

## Release evidence

Every candidate and activation receipt records backend/frontend commits and trees, database migration/package hashes, catalog fingerprint, Qdrant collection/alias/configuration, service/unit hashes, test command and result, synthetic owner IDs, provider-call count, rollback identity, and residual risk. Source, tests, installed units, and manifests do not substitute for invoked-and-consumed route proof.

## Legacy-test separation

The following files are temporary behavioral references only. Their assertions must be mapped into the clean suite; the versioned files are then removed:

```text
tests/test_memory_actor_auth_v1.py
tests/test_memory_prompt_renderer_v1.py
tests/test_memory_v1_answer_provenance_v1.py
tests/test_memory_v1_evidence_intake_dispatcher.py
tests/test_memory_v1_extraction_job_store_v1.py
tests/test_memory_v1_governed_claim_lifecycle_router_v1.py
tests/test_memory_v1_governed_claim_transition_v1.py
tests/test_memory_v1_governed_postgres_loader_v2.py
tests/test_memory_v1_openai_circuit_window_v1.py
tests/test_memory_v1_openai_eligibility_disposition_v2.py
tests/test_memory_v1_openai_exact_job_claim_v1.py
tests/test_memory_v1_openai_extraction_worker_v1.py
tests/test_memory_v1_openai_postgres_authority_v1.py
tests/test_memory_v1_openai_provider_adapter_v1.py
tests/test_memory_v1_openai_review_admission_v1.py
tests/test_memory_v1_openai_review_packet_v1.py
tests/test_memory_v1_openai_structured_transport_v1.py
tests/test_memory_v1_personal_evidence_exchange_v2.py
tests/test_memory_v1_personal_evidence_prefilter_v1.py
tests/test_memory_v1_projection_exact_outbox_v1.py
tests/test_memory_v1_projection_outbox_release_contract_v1.py
tests/test_memory_v1_projection_payload_policy.py
tests/test_memory_v1_qdrant_rebuild_contract_v1.py
tests/test_memory_v1_queue_reconciliation_v1.py
tests/test_memory_v1_selection_envelope_v1.py
tests/test_memory_v1_selection_schema_v1.py
tests/test_governed_memory_provider_v1.py
```

Five additional cutover guards remain only until the old runtime is absent:

```text
tests/test_memory_v1_active_runtime_manifest_v1.py
tests/test_memory_v1_active_runtime_verifier_v1.py
tests/test_memory_v1_governed_activation_verifier_v1.py
tests/test_memory_v1_contextual_chat_capture_bridge_v1.py
tests/test_memory_v1_raw_memory_compatibility_boundary_v1.py
```

After the clean suite passes, the active branch removes all 153 paths in the current `tests/test_memory*.py` snapshot, all 170 Memory SQL fixtures, the Memory JSON fixture, the remaining Memory helper/clone fixtures, and all 34 committed `evals/memory_v1*` artifacts. Before deletion, materialize their exact paths and Git blob hashes. Future deletion must use that frozen manifest, never a wildcard.

One unreadable root-owned test contains user-like historical text and UUIDs, duplicated in two readable tests. Those fixtures must not be copied. All successor fixtures are synthetic, provenance-labelled, and contain no production-derived chat, attachment, person, pet, employment, project, philosophy, claim, or account material.

Frontend attachment tests consolidate into `tests/chatAttachments.test.ts`. Create `tests/memorySuccessorBoundary.test.ts`, `tests/memoryLifecycleUi.test.ts`, `tests/adminMemorySystem.test.ts`, and `tests/attachmentMemoryBoundary.test.ts`. Remove the legacy Memory/Vantage source-regex tests only after these tests and a clean production build pass. Privacy-control tests remain deferred with the separate Delete-All workstream.
