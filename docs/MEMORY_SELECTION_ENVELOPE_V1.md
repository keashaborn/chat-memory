# MemorySelectionEnvelopeV1

Status: frozen Memory V1 interface; isolated and not wired to shared production prompt code.

Normative versions:

- request: `memory_selection_request_v1`
- request binding: `memory_selection_request_binding_v1`
- selector: `memory_v1_authoritative_selector_v1`
- envelope: `memory_selection_envelope_v1`
- budget: `memory_selection_budget_v1`
- token estimator: `memory_prompt_token_estimator_v1`
- rejection registry: `memory_selection_rejection_registry_v1`
- embedding artifact: `memory_query_embedding_artifact_v1`
- prompt-assembly context/input: `memory_prompt_assembly_context_v1` / `memory_prompt_assembly_input_v1`
- final-answer binding: `final_answer_memory_binding_v1`

## Purpose

`MemorySelectionEnvelopeV1` is the sole typed boundary between governed Memory V1 selection and prompt assembly. It contains typed governed records, zero-token controls, policy, provenance references, bounded selection metadata, and integrity bindings. It never returns `prompt_block`, `memory_chunks`, or rendered prompt prose.

The executable contract is `rag_engine/memory_v1_selection_envelope.py`. Exact generated wire schemas are:

- `specs/memory_selection_request_v1.schema.json`
- `specs/memory_selection_envelope_v1.schema.json`
- `specs/memory_prompt_assembly_input_v1.schema.json`
- `specs/final_answer_memory_binding_v1.schema.json`

Regenerate them on seebx with:

```bash
cd /home/ubuntu/chat-memory-memory-interface-v1
/opt/chat-memory/venv/bin/python scripts/export_memory_v1_selection_contract.py
```

All wire versions are required literals. Unknown fields and coercive values fail closed. `model_validate_json()` or the supplied `from_wire_json()` wrappers are the supported strict wire parsers; callers must not parse JSON into a Python dictionary and then rely on non-wire coercion.

## Authoritative path

The only production-eligible governed selection call is:

```python
envelope = await select_governed_memory_v1(memory_owned_selector, request)
```

`AuthoritativeGovernedMemorySelectorV1` is the Memory-owned composition root. Its lane-provider injection exists for internal composition and tests; shared runtime code must not call lane providers directly or construct an envelope from provider results.

The selector reparses the request and every provider result so bypass-created Pydantic instances cannot skip validators. It then independently reapplies owner, lane-specific source-contract role/version, sensitivity, temporal, supersession, explicit-recall, content-policy, exact project/component scope, token, lane, global-record, and global-control rules. It deduplicates logical heads rather than treating multiple revisions as separate memories, assigns final canonical ranks, and returns one manifest-bound envelope.

## Complete request binding

`MemorySelectionRequestBindingV1` hashes every selection-affecting input without retaining the query or vector:

- all request, selector, intent-adapter, source-contract, budget, and estimator versions;
- selection trace, authenticated actor, owner, request hash, thread ID/hash, and query hash;
- requested lanes, selection directive, memory intent, domains, explicit-recall flag, and sensitivity ceiling;
- project and optional component scope;
- frozen selection timestamp;
- complete global/lane/control budget policy;
- immutable embedding source, model version, dimension, vector hash, and upstream external-call count.

The internal selector request carries query text and the frozen vector as excluded fields. They never appear in the envelope or sanitized trace. A claim-selection request fails unless its vector matches the frozen embedding artifact exactly. Identical replay requires the caller to reuse the same trace ID and selection timestamp; changed selection inputs produce a different request-binding hash.

## Typed content and controls

The three content lanes are:

| Lane | Record | Scope |
|---|---|---|
| `claim` | `ClaimSelectionV1` | Governed claim with epistemic status and evidence grouped by stance. |
| `preference` | `LifePreferenceSelectionV1` | User life preference that may be content only when relevant. |
| `project_knowledge` | `ProjectKnowledgeSelectionV1` | Governed project record restricted to exact project/component scope. |

Response-style influence is a separate `ResponsePreferenceControlV1`. Controls are typed, evidence-backed, zero-token, capped at eight, and never rendered as user facts. The V1 action registry is intentionally closed; new response-control semantics require a new reviewed contract version.

Every selected content record repeats the verified owner and carries stable record/revision IDs, a named and versioned source contract, `source_content_sha256`, canonical rank, typed surface/use policy, sensitivity, temporal interval, supersession pointer, evidence/observation references, independent salience dimensions, and a selector-computed token estimate.

`source_content_sha256` identifies the governed source representation. It is not the envelope-integrity hash. `envelope_sha256` covers the complete selected typed payload.

Claim and project selections require evidence and observation provenance. A supported claim requires supporting evidence, and its flat evidence set must equal the union of stance-specific evidence. Life-preference values are immutable canonical JSON strings rather than mutable arbitrary objects.

The preference/project enums match the installed V5 contracts. `LifePreferencePolarity` is the selectable life-content subset; installed `not_applicable` rows remain non-selectable. Surface/use-instruction pairs use a closed compatibility registry, and uncertain/disputed claims require the uncertainty/counterevidence instruction. Root project records may have both component ID and component key null; one-null/one-set is invalid. A superseded project document is never selectable.

## Ownership and account compatibility

The authentication chain is:

```text
Supabase bearer token verified by Verbal Sage
  → trusted BFF service request with actor user ID
  → backend actor/owner equality check
  → controlled Postgres owner scope and row revalidation
  → typed selector request repeats actor/owner equality
  → envelope and every nested record/control repeat owner equality
  → prompt-assembly context repeats the authenticated actor
```

`vantage_id`, persona, FM state, browser cookies, thread labels, Qdrant payloads, and client-selected response settings cannot establish or change memory ownership. Thread ID is an audit/request binding, not an ownership dimension or a durable project-memory filter.

Qdrant may suggest candidate IDs only. Every candidate must be reloaded and authorized in Postgres. Missing, stale, or cross-owner candidates fail closed. An account with no governed records receives a valid empty envelope; there is no cross-account or legacy-owner fallback.

## Budget and estimator

The standard initial policy is:

```text
global content:    8 records, 600 estimated tokens
claims:            4 records, 500 estimated tokens
life preferences:  3 records, 160 estimated tokens
project knowledge: 4 records, 600 estimated tokens
controls:           8 zero-token controls
hard contract cap: 8 records, 1200 tokens, 8 controls
```

`MemorySelectionBudgetPolicyV1.standard()` supplies this initial preset. Alternate reviewed policies may vary within the hard contract caps. Global caps win when lane caps overlap. The selector ignores provider-asserted token estimates and recomputes each estimate using `memory_prompt_token_estimator_v1`. This estimate is only for selection. The final-answer binder records actual renderer-reported prompt tokens and fragment hashes.

## Rejection and observability

Rejection codes are a closed versioned enum, not free prose. Observability separates:

- `primary_rejection_counts`: exactly one primary result per unselected content candidate;
- `reason_counts`: nonexclusive diagnostic content reasons;
- `control_primary_rejection_counts`: exactly one primary result per unselected control;
- `control_reason_counts`: nonexclusive control diagnostics.

Primary totals must reconcile with candidate minus selected counts. The envelope also records lane outcomes, candidate/selection/control hashes, embedding-artifact hash and upstream external embedding-call count, Qdrant's role, owner verification, and `postgres_revalidation=performed|not_needed`. Suppressed turns use `not_needed`; evaluated turns use `performed`. Selector database writes, Qdrant writes, and selector external-model calls are literal zero.

`sanitized_trace()` creates detached structures containing only versions, hashes, counts, bounded outcomes, budgets, and closed rejection codes. It excludes query/vector data, clear owner ID, record/control handles, claim/project prose, preference keys/values, evidence content, prompt content, and FM content.

## Suppression and errors

Suppression is an explicit typed request directive. A suppressed request has no lanes, performs no provider calls, and returns a manifest-bound zero-candidate `suppressed` envelope. Evaluation requires at least one lane.

Selector errors are out-of-band exceptions. There is no in-band `error` envelope that prompt assembly could accidentally treat as valid memory.

## Prompt interface

Prompt assembly receives exactly:

```python
MemoryPromptAssemblyInputV1(
    context=MemoryPromptAssemblyContextV1(...authenticated bindings...),
    envelope=MemorySelectionEnvelopeV1(...),
)
```

The input validates actor/owner, selection trace, request/thread/query hashes, request-binding hash, envelope hash, `context_sha256`, `assembly_input_sha256`, supported versions, renderer version, and renderer token cap. The response-policy task owns the output representation and final cross-subsystem ordering. It does not own Memory candidate selection or owner filtering.

## Final-answer binding

Every successfully generated answer will receive one append-only `FinalAnswerMemoryBindingV1`, including answers produced from an empty envelope. Inspect-only calls and failed answer generation retain selection audit but do not create an answer binding.

V1 records these verifiable stages:

```text
answer_model_exposed ⊆ injected ⊆ selected
```

The renderer reports each injected record using `InjectedMemoryRecordV1`, including actual prompt-token count and rendered-fragment SHA-256. The binder records the assembly context/input hashes, renderer version, and assembly token cap, and checks actual tokens against that exact cap. It rejects duplicate, unknown, cross-owner, wrong-revision, wrong-hash, exposed-but-not-injected, and over-budget reports.

V1 has no caller-asserted attribution field or `attribution_reported` outcome. Model attribution can be added only with structured evidence in a later version. Binding control references omit preference keys and retain only nonsemantic stable handles and hashes.

The future database writer must use a new forced-RLS, append-only ledger with composite owner keys and restricted writer functions. Identical replay for the same owner/trace and owner/answer writes zero rows; a conflicting manifest fails. Existing mutable `memory.retrieval_trace` and `public.vantage_answer_trace` are not authoritative for this binding.

## Explicit exclusions

This contract does not implement or decide:

- production response modes;
- client `mix.lens_fm` retirement;
- FM eligibility, projection, selection, or high-stakes/application vetoes;
- FM v0.2 compilation;
- final cross-subsystem prompt ordering;
- nutrition or training structured-data adapters;
- production retrieval or prompt activation.

FM never enters the request or envelope, supplies evidence, changes owner scope, or filters governed candidates. Nutrition/training adapters remain separate and disabled until their schemas and data coverage are audited, redesigned, and finalized. Neither is a blocker to the stability of this Memory-owned interface.
