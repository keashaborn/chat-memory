# Memory V1 Interface Readiness Handoff

`MEMORY_INTERFACE_READY: YES`

Date: 2026-07-20

This verdict applies to the versioned typed Memory V1 selection and audit interface. It does not activate retrieval, prompt influence, FM selection, or shared runtime wiring.

## Isolated implementation

- server: seebx
- worktree: `/home/ubuntu/chat-memory-memory-interface-v1`
- branch: `memory_v1_interface_v1`
- base production commit: `664a93c9d22b7320a3b2343165993fda12006e08`
- executable contract commit: `dd97a8387d15b59fbcc92927bce34f0530c30cfe`
- zero-work suppression hardening commit: `400b370eb898d159c3cf066f88287e77baa83b73`

The production worktree `/opt/chat-memory` remained clean at the base commit during this phase. No service, timer, database row/schema, Qdrant collection, prompt, frontend file, or production configuration changed.

## Frozen deliverables

- `rag_engine/memory_v1_selection_envelope.py`
- `scripts/export_memory_v1_selection_contract.py`
- `specs/memory_selection_request_v1.schema.json`
- `specs/memory_selection_envelope_v1.schema.json`
- `specs/memory_prompt_assembly_input_v1.schema.json`
- `specs/final_answer_memory_binding_v1.schema.json`
- `tests/test_memory_v1_selection_envelope_v1.py`
- `tests/test_memory_v1_selection_schema_v1.py`

Generated schema SHA-256 values:

```text
54c1809a748776ed5255c1b6f031699e84740f5617ef314db295b6877d526f16  memory_selection_request_v1.schema.json
0a7ae57c2c288ea8f3eaff68a3a019aa97d3255f667def3152f48c176a06d649  memory_selection_envelope_v1.schema.json
360d90120ae7cca98c6bc0df8d4a8ac4ac87296e2fd2e1355550682a7203c5a3  memory_prompt_assembly_input_v1.schema.json
b3a769cf8fe6152ad506ab9f791aefe12f51a2e09546a2726e9a08171dd64a1e  final_answer_memory_binding_v1.schema.json
```

## Interface result

The contract now provides:

- one versioned `MemorySelectionRequestV1` with an explicit sensitive wire serializer/parser;
- one immutable request binding covering every selection-affecting field;
- one authoritative selector facade with a Memory-owned production composition requirement;
- typed claim, life-preference, project-knowledge, and zero-token control records;
- lane-specific source-contract roles and current V5 enum compatibility;
- actor/owner equality repeated at request, provider, envelope, assembly, and final-binding boundaries;
- independent source, sensitivity, temporal, supersession, explicit-recall, scope, surface/use, evidence, deduplication, and budget enforcement;
- deterministic selector-computed estimates and separate renderer-reported actual prompt tokens;
- closed versioned rejection/observability fields with reconciled content/control counts;
- typed suppression with zero provider reads and truthful Postgres revalidation state;
- manifest-bound `MemoryPromptAssemblyContextV1` and `MemoryPromptAssemblyInputV1`;
- final-answer binding of selected, injected, exposed, and applied-control stages;
- no unverifiable model-attribution claim in V1;
- strict generated JSON schemas with required versions and closed objects.

## Verification evidence

The isolated contract/schema suite passed:

```text
42 tests, 0 failures
```

It includes adversarial tests for forged Pydantic instances, reconstructed cross-owner wire data, complete request-hash differentiation, source-lane swaps, temporal/supersession state, sensitivity, explicit recall, project/component root scope, logical-head revision deduplication, evidence-stance exclusivity, provider token underreporting, lane/global/control budgets, control ordering, sanitized trace detachment, assembly manifest/cap binding, actual renderer tokens, empty-answer binding, and semantic tampering with recomputed outer manifests.

Existing Memory V1 compatibility checks also passed:

```text
memory_v1_v5_shadow_candidate: PASS
memory_v1_v5_shadow_retrieval: PASS
memory_v1_v5_shadow_trace: PASS
memory_v1_preference_project_retrieval: PASS
memory_v1_preference_project_shadow: PASS
V5 project shadow trace unit tests: 4 passed
```

## Exact consumer boundary

The response-policy task may now target this stable input:

```python
memory_input = MemoryPromptAssemblyInputV1.create(
    context=MemoryPromptAssemblyContextV1.from_envelope(
        envelope=envelope,
        authenticated_actor_user_id=trusted_actor_user_id,
        renderer_version="...",
    ),
    envelope=envelope,
)
```

The response-policy-owned renderer must not construct lane providers, select Memory candidates, change owner scope, accept legacy chunks/`prompt_block`, or make FM a Memory filter. It reports injected record fragments/tokens, exposed refs, and applied control refs to the central finalizer.

## Remaining production wiring

These are implementation gates, not typed-interface blockers:

1. Build one Memory-owned configured selector factory and concrete claim/preference/project adapters.
2. Add the deterministic query-embedding and intent/source-pin adapters.
3. Prove each adapter under the production-schema clone, restricted runtime role, and forced RLS.
4. Add restricted append-only selection and final-answer binding ledgers with composite owner keys and zero-write replay; require an immutable answer foreign key or add an answer-content hash.
5. Separate nonpersonal corpus retrieval from the current mixed `memory_chunks` path.
6. Run universal zero-influence shadow comparison using sanitized hashes/counts plus the restricted audit ledger.
7. Jointly wire `MemoryPromptAssemblyInputV1` into shared prompt construction for one allowlisted owner.
8. Route test, ritual, greeting, and normal answer paths through one final-answer binder.
9. Expand owner-by-owner after accuracy, token, empty-rate, rollback, and account-isolation gates pass.
10. Retire legacy personal chunks, old Memory `prompt_block` strings, and card contributors after parity.

Shared production prompt/request files remain unchanged and require separate joint integration approval.

## Cross-subsystem boundaries

Production response-mode implementation, client-controlled `mix.lens_fm` removal, FM high-stakes/application vetoes, FM projection/selection, FM v0.2 compilation, and final prompt ordering remain response-policy work. FM does not enter or filter the Memory request/envelope and never supplies Memory evidence or ownership.

Nutrition and training structured-data integration remains separate and disabled until those tables, data coverage, and semantics are audited, redesigned, and finalized. This deferral does not change `MEMORY_INTERFACE_READY: YES`.
