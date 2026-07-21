# Memory V1 Authoritative Selector Cutover Plan

Status: typed interface ready in isolation; production composition, persistence, retrieval, and prompt wiring withheld.

Base commit: `664a93c9d22b7320a3b2343165993fda12006e08`

Isolated worktree: `/home/ubuntu/chat-memory-memory-interface-v1`

Branch: `memory_v1_interface_v1`

## Target

All governed Memory V1 content/control selection reaches shared runtime through one Memory-owned configured call:

```python
envelope = await select_governed_memory_v1(configured_memory_selector, request)
memory_input = MemoryPromptAssemblyInputV1.create(
    context=trusted_assembly_context,
    envelope=envelope,
)
```

The response-policy renderer receives `MemoryPromptAssemblyInputV1`, never a bare lane result or pre-rendered Memory string. A production Memory-owned selector factory/composition hook remains required. Shared runtime and response-policy code must not instantiate lane providers or choose their composition.

Thread context, nonpersonal corpus retrieval, LifeSwitch structured-data adapters, and FM remain separate prompt contributors. The cutover must preserve ordinary nonpersonal corpus retrieval while retiring legacy personal-memory chunks.

## Current contributor inventory

| Contributor | Current path | Disposition |
|---|---|---|
| Legacy raw episodic memory | `vantage_router.py` → `retrieve_personal_memory()` → `memory_chunks` | Replace with typed envelope, then disable and retire. |
| Nonpersonal corpus | `vantage_router.py` → `unified_retrieve()` → `memory_chunks` | Split from personal memory and preserve as a separate typed corpus contribution. |
| Qdrant memory-card instructions | `prompt_builder.py` → `build_user_instructions_block()` | Compare, migrate valid governed controls, then retire. |
| Legacy preference/style cards | `prompt_builder.py` → `build_vantage_preference_cards_block()` | Replace permitted behavior with governed controls or response policy, then retire. |
| Legacy profile/project cards | `prompt_builder.py` → `build_vantage_profile_cards_block()` | Replace with claim/project lanes, then retire. |
| Older governed V1 | `run_memory_v1_runtime()` → `prompt_block: str` | Adapt to claim provider; never forward the string. |
| Older specialized V1 | `run_preference_project_runtime()` → `prompt_block: str` | Adapt to preference/project providers; never forward the string. |
| V5 claim shadow | candidate → owner-scoped loader → evaluator → sanitized trace | Refactor into claim provider without discarding selected typed records. |
| V5 project shadow | owner/project loader → hashed trace | Refactor into project provider without discarding selected typed records. |

The current V5 shadow paths discard fields needed by the envelope after producing sanitized traces. Concrete adapters must preserve owner, named source contract/version, source-content hash, sensitivity, temporal state, exact scope, policy, provenance, and governed typed content until the authoritative selector finishes.

## Account compatibility and authentication

The trusted chain is:

1. Verbal Sage verifies the Supabase bearer token/user.
2. The BFF sends a service-authenticated request with the actor user ID.
3. Backend `require_actor_matches_owner` compares that trusted actor header with requested owner; it does not itself verify the Supabase JWT.
4. Controlled Postgres readers set and enforce the same owner scope and revalidate every row.
5. `MemorySelectionRequestV1`, the envelope, the assembly context/input, and final binding repeat owner equality.

Migration rules:

1. Existing activation flags and prompts remain unchanged during shadow phases.
2. Accounts without governed records receive a manifest-bound empty envelope and never borrow another account's records.
3. Activation is allowlisted by authenticated user ID, never `vantage_id`, persona, cookie, FM state, or thread.
4. Thread ID is an audit/request binding, not an owner filter and not a durable project-memory boundary.
5. Rollback disables the new selector/renderer path without changing governed source records.
6. Legacy fallback is isolated during comparison and removed after parity; legacy and governed memory are never merged in one contribution.

## Phased migration

### Phase 1 — Frozen typed interface

Complete in the isolated branch:

- strict request and complete immutable request binding;
- closed typed envelope, record/control policy, budgets, rejection/observability, and manifests;
- immutable query-embedding artifact with upstream-call accounting;
- one selector facade that reparses providers and reapplies policy/budgets;
- typed prompt-assembly context/input with actor and manifest binding;
- final-answer binding using actual renderer tokens/fragments and no unverifiable attribution;
- four deterministic generated JSON schemas;
- adversarial owner, instance-bypass, temporal, source, scope, budget, suppression, trace, and binding tests;
- no shared runtime, database, Qdrant, service, timer, or frontend change.

### Phase 2 — Concrete governed lane adapters

Still isolated until clone tests pass:

- create one Memory-owned configured selector factory;
- adapt V5 claim candidate discovery, Postgres reload, provenance, and policy selection;
- adapt governed life preferences and zero-token response controls;
- adapt V5 project loader using owner plus exact project/optional component scope;
- add intent-adapter, source-pin, deterministic token, and query-embedding adapters;
- retain selected typed fields until envelope construction;
- run production-schema-clone tests using the restricted runtime role and forced RLS;
- prove no selector database/Qdrant writes and no selector external-model calls;
- when an upstream embedding call occurs, freeze and audit its artifact before selection.

### Phase 3 — Append-only selection and answer ledgers

Additive migration requiring a separate production review:

- restricted forced-RLS request/envelope event and selected-item ledger;
- restricted forced-RLS final-answer binding event and item ledger;
- composite owner foreign keys and append-only mutation guards;
- unique/replay rules for `(owner_user_id, selection_trace_id)` and `(owner_user_id, answer_id)`;
- identical replay writes zero rows; conflicting replay fails closed;
- no query, answer, prompt, claim, preference, project, evidence, or FM prose.

Two observability layers remain distinct:

- general operational traces persist sanitized hashes, counts, outcomes, latency, and closed rejection codes;
- the restricted forced-RLS ledger persists stable governed handles/hashes needed for exact audit.

Neither layer stores governed prose.

### Phase 4 — Universal shadow comparison

Shared runtime change requiring joint approval:

- build one request after trusted authenticated owner binding;
- invoke only the configured authoritative selector;
- persist sanitized operational audit and restricted ledger events;
- do not pass the envelope to the answer model;
- compare counts/hashes, rejection distribution, latency, and empty rates with existing routes;
- compare clear handles only in restricted in-memory tests, not general operational traces;
- include dense-history, sparse-history, and no-memory accounts;
- prove unchanged prompt behavior and account isolation.

### Phase 5 — Prompt canary

Shared runtime change requiring joint approval:

- construct the trusted `MemoryPromptAssemblyContextV1` and `MemoryPromptAssemblyInputV1`;
- render for one allowlisted owner while preserving thread/corpus contributors separately;
- return actual fragment hashes/tokens, exposed refs, and applied controls;
- route test, ritual, greeting, and normal answer branches through one finalizer;
- bind every successfully generated answer, including an empty envelope;
- compare accuracy, false recall, over-personalization, token use, and isolation;
- rollback by disabling prompt eligibility without deleting governed data.

### Phase 6 — Account expansion

- expand the allowlist one owner at a time;
- require zero cross-owner records and deterministic empty-envelope behavior;
- verify legacy and governed memory never contribute together;
- keep nutrition/training adapters disabled until their tables, coverage, and semantics are audited, redesigned, and finalized.

### Phase 7 — Legacy retirement

After universal parity:

- disable legacy raw personal-memory retrieval;
- disable old governed/specialized `prompt_block` contributors;
- disable card-based user instructions, preferences, profiles, and project context;
- keep nonpersonal corpus retrieval under its separate contract;
- update route-retirement tests;
- move unused modules to an explicit retired package or delete them in a reviewed cleanup commit;
- remove obsolete frontend controls only after their remaining consumers are mapped.

## Shared production files withheld from this branch

Backend files requiring joint Memory V1 review during later wiring include:

- `rag_engine/vantage_router.py`
- `rag_engine/prompt_builder.py`
- `rag_engine/persona_loader.py`
- `rag_engine/retriever_unified.py`
- `rag_engine/vantage_query_support.py`
- `rag_engine/lifeswitch_auth.py`
- `rag_engine/query_embedding_cache.py`
- `rag_engine/memory_v1_intent.py`
- `rag_engine/memory_v1_shadow.py`
- `rag_engine/memory_v1_retrieval.py`
- `rag_engine/memory_v1_preference_project_shadow.py`
- `rag_engine/memory_v1_preference_project_retrieval.py`
- `rag_engine/memory_v1_v5_shadow_candidate.py`
- `rag_engine/memory_v1_v5_shadow_loader.py`
- `rag_engine/memory_v1_v5_shadow_retrieval.py`
- `rag_engine/memory_v1_v5_shadow_trace.py`
- `rag_engine/memory_v1_v5_project_shadow_loader.py`
- `rag_engine/memory_v1_v5_project_shadow_trace.py`
- the future configured-selector factory, ledger writer/migration, activation config, and central answer finalizer;
- `scripts/legacy_rag_route_retirement_test.py`
- `scripts/legacy_route_retirement_test.py`

Frontend compatibility review includes:

- Verbal Sage `app/api/chat/route.ts`
- Verbal Sage `app/api/chat/inspect/route.ts`
- Verbal Sage `app/api/_brains/headers.ts`
- Verbal Sage `app/api/_auth/supabaseUser.ts`

No shared production file is modified in the interface branch.

## Remaining gates before prompt activation

- production Memory-owned selector factory exists and cannot be composed by response policy;
- concrete claim/preference/project adapters pass production-schema-clone forced-RLS tests;
- append-only selection/final-binding persistence and replay tests pass;
- universal shadow metrics show deterministic isolation and acceptable empty/selection rates;
- nonpersonal corpus is separated from legacy personal `memory_chunks`;
- all answer paths use the central finalizer;
- no legacy personal/card/prompt-block contributor runs when the new renderer is active;
- rollback is tested without deleting governed records;
- joint Memory V1/response-policy review approves shared-file wiring and final ordering.

Production response modes, client `mix.lens_fm` retirement, FM high-stakes/application veto, FM projection/selection, and final cross-subsystem ordering remain response-policy work. They do not block the frozen Memory interface. FM never owns, filters, extracts, or supplies evidence to Memory V1.
