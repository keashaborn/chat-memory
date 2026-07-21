# Memory V1 Prompt-Assembly Boundary

Status: typed input frozen; response-policy-owned rendering and shared runtime wiring remain disabled.

## Normative flow

```text
verified Supabase actor
  → MemorySelectionRequestV1
  → select_governed_memory_v1(memory_owned_selector, request)
  → MemorySelectionEnvelopeV1
  → MemoryPromptAssemblyContextV1
  → MemoryPromptAssemblyInputV1
  → response-policy-owned memory renderer
  → response-policy-owned typed contribution
  → final prompt assembly
  → answer
  → FinalAnswerMemoryBindingV1
```

There is no parallel `prompt_block`, `memory_chunks`, card block, or direct lane-provider output in this path.

## Exact interface

The shared prompt integration must expose a single memory renderer port equivalent to:

```python
def render_governed_memory_v1(
    *,
    memory_input: MemoryPromptAssemblyInputV1,
):
    """Return the response-policy-owned typed memory contribution."""
```

The output type and final ordering are intentionally not defined by Memory V1. The input type is fully defined and binds:

- authenticated actor and owner;
- request, thread, query, selection-trace, request-binding, and envelope hashes/IDs;
- supported request/envelope/selector versions;
- the verified immutable envelope;
- a renderer version and maximum memory prompt-token cap.
- manifest-bound `context_sha256` and `assembly_input_sha256` values.

An envelope by itself is not a sufficient assembler input. `MemoryPromptAssemblyContextV1` must be constructed from the trusted authenticated request context, not accepted from a browser as an owner assertion.

## Boundary ownership

Memory V1 owns:

- actor/owner equality repeated after the trusted BFF authentication chain;
- memory intent, requested lanes, and explicit memory suppression;
- governed source loading, Postgres authorization, and row revalidation;
- candidate selection, deduplication, temporal/supersession handling, and exact project scope;
- sensitivity, content-surface/use policy, and content/control budgets;
- record/control provenance, versions, immutable handles, and integrity hashes;
- sanitized selection audit and final-answer memory binding.

Response policy owns:

- response mode;
- rendering selected typed records into compact model-facing content;
- applying selected zero-token response controls without surfacing them as facts;
- ordering memory relative to safety, thread, nonpersonal corpus, FM, and current user content;
- reporting actual injected fragments/tokens, model exposure, and applied controls to the binder;
- choosing whole-contribution non-injection only under the jointly reviewed integration policy, with that non-injection recorded.

Response mode and memory intent remain separate. Response policy may not use FM, persona, or client-selected mode as a Memory owner, evidence source, candidate source, or record-level filter. A high-stakes response mode does not by itself suppress relevant governed health/account facts.

FM owns only its separate semantic corpus and application policy. FM is never a memory owner, extractor, candidate source, evidence source, filter, lane, request field, or envelope field. This work does not compile or select FM v0.2.

## Required assembler validation

`MemoryPromptAssemblyInputV1` fails closed when:

- authenticated actor or context owner differs from envelope owner;
- trace, request, thread, query, request-binding, or envelope binding differs;
- supported contract/selector versions differ;
- the renderer budget exceeds the selected envelope budget;
- the assembly context or input manifest differs;
- the nested envelope manifest or any nested semantic invariant fails.

After accepting the typed input, the renderer must preserve canonical rank, honor typed surface/use policy, never render controls as user facts, and enforce the token cap again. It performs no retrieval, owner resolution, database write, Qdrant request, external-model call, or memory-policy reevaluation. Rejection does not silently fall back to legacy personal memory.

## Contribution reporting and finalization

The response-policy renderer returns an internal report containing:

- `InjectedMemoryRecordV1` for every rendered record, with actual token count and fragment hash;
- selected record references actually exposed to the answer model;
- selected control references actually applied.

One central answer finalizer then creates `FinalAnswerMemoryBindingV1` from the exact `MemoryPromptAssemblyInputV1`. The binding records the assembly context/input hashes, renderer version, and assembly token cap; actual tokens are checked against that cap. The binder enforces:

```text
answer_model_exposed ⊆ injected ⊆ selected
applied_controls ⊆ selected_controls
```

V1 deliberately records no model attribution. Selection, injection, and exposure are auditable facts; they are not proof that the model relied on a record.

All answer-producing branches must converge on that finalizer:

- test mode;
- deterministic ritual bypass;
- greeting bypass;
- normal model generation.

Empty envelopes are bound to generated answers so zero exposure is provable. Inspect-only requests and failed model calls do not create final-answer bindings.

## Separate prompt contributors

The following never enter `MemorySelectionEnvelopeV1`:

- recent thread messages;
- nonpersonal corpus passages;
- LifeSwitch nutrition/training database results;
- FM semantic selections;
- safety or response-mode instructions;
- assistant-profile/persona settings;
- raw user query or prompt text.

Each requires its own typed contribution and budget. Nonpersonal corpus retrieval must be separated from the current mixed `memory_chunks` path before legacy personal memory is retired. LifeSwitch nutrition/training adapters remain disabled until their underlying tables and coverage are reviewed and finalized.
