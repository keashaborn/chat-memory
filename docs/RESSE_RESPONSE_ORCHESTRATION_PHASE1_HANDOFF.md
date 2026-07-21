# RESSE Response Orchestration Phase 1 Handoff

Status: complete, isolated, not activated

## Repository state

- Server: seebx
- Worktree: `/home/ubuntu/chat-memory-resse-response-v0_2`
- Branch: `resse_response_orchestration_v0_2`
- Production base: `664a93c9d22b7320a3b2343165993fda12006e08`
- Phase 1 implementation tip before this handoff document: `7d3bf4cf309acaaeb8d7dc2dbdcb8a08eb32b522`
- Live checkout during final verification: `/opt/chat-memory` at `664a93c9d22b7320a3b2343165993fda12006e08`, clean except for its existing branch-ahead relationship to origin

## Delivered boundaries

### Backend-owned response policy

`rag_engine/response_policy_v0_2.py` creates a deterministic, request-bound decision from a complete server safety assessment and trusted server signals. The fixed precedence is:

```text
HIGH_STAKES > TECHNICAL > FM_EXPLICIT > COACHING > ORDINARY
```

The policy fixes the assistant identity as RESSE, ignores legacy identity/tuning request fields, keeps governed Memory and structured data independent, applies user FM opt-out, and makes the response closure explicit. High-stakes and technical modes force FM off. Local risk detection is defense in depth; it does not replace the required safety assessment.

`rag_engine/response_policy_prompt_v0_2.py` compiles only a validated decision into compact instructions. It does not classify, retrieve, call a provider, or assemble the final request.

### Canonical FM v0.2 projection

`scripts/compile_fm_runtime_bundle_v0_2.py` compiles the canonical local semantic package into `rag_engine/data/fm_v0_2_runtime_bundle.json`.

Pinned identities:

- Canonical manifest SHA-256: `1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d`
- Compiled bundle SHA-256: `a50a256e5d8b84d7521b816054019ab09a4f5a0e4e31b12bf4ecd036f4e002fe`
- Runtime records: 198: 46 concepts, 48 relationships, 22 historical formulations, 27 inference rules, 27 applications, and 28 tensions

`rag_engine/fm_runtime_bundle_v0_2.py` validates the pinned bundle without YAML, database, retrieval, provider, or network dependencies.

`rag_engine/fm_selection_envelope_v0_2.py` produces a deterministic, typed, auditable selection bound to the exact policy decision and current message. OFF modes return no FM content. LIGHT modes are allowlisted and capped. Explicit mode preserves concepts, tensions, inference rules, epistemic labels, competing interpretations, application boundaries, and provenance. Historical formulations are never silently promoted to current doctrine.

An active LIGHT or EXPLICIT policy requires a selector envelope. EMPTY is the auditable no-match result for LIGHT. FM_EXPLICIT requires selected canonical content.

### Governed Memory V1 renderer

`rag_engine/memory_v1_selection_envelope.py` provides the frozen Memory V1 selection and prompt-boundary types.

`rag_engine/memory_prompt_renderer_v1.py` implements a two-stage pure boundary:

```text
MemoryPromptAssemblyInputV1
  -> MemoryPromptRenderResultV1
  -> MemoryControlApplicationDecisionV1
  -> MemoryPromptApplicationResultV1
```

The renderer reports candidates only. Only the application result may claim that exact fragments were included. The direct-relevance control is applied atomically. A false decision suppresses content while retaining an auditable control result. Exact UTF-8 fragments, token estimates, record references, request bindings, and manifests are retained without exposing governed prose in sanitized reports.

### Typed prompt assembly

`rag_engine/prompt_assembler_v1.py` accepts only the typed policy chain, an optional paired Memory input/application, and an FM selector envelope when FM is active. It produces:

```text
backend safety/response policy     system authority
governed Memory                    separate reference-data block
canonical FM v0.2                  separate reference-data block
conversation                       user/assistant messages
sanitized assembly manifest        hashes, counts, budgets, source bindings
```

There is no generic contribution or caller-authored hidden-context interface. The obsolete `rag_engine/prompt_contribution_v1.py` was removed.

The assembler independently recomputes:

- the response-policy decision and policy prompt;
- the Memory render and post-control application;
- the deterministic FM selection;
- the exact system prompt, reference blocks, conversation, manifests, and budgets.

The internal assembled artifact retains its typed source request so a serialized result cannot be rehashed into a different authority chain. Public wire failures are generic, private content is omitted from representations and sanitized manifests, and duplicate JSON keys fail closed.

Current budgeting is provider-neutral and conservative: 96,000 total UTF-8 input bytes, 32,000 estimated input tokens, an 8,000-token output reservation, and a 128,000-token context commitment. Exact provider tokenizer accounting is intentionally deferred to live integration.

## Validation

### Aggregate unit suite

Server: seebx, isolated worktree

```bash
/opt/chat-memory/venv/bin/python3 -m unittest \
  tests.test_response_policy_v0_2 \
  tests.test_response_policy_prompt_v0_2 \
  tests.test_fm_runtime_bundle_v0_2 \
  tests.test_fm_selection_envelope_v0_2 \
  tests.test_memory_v1_selection_schema_v1 \
  tests.test_memory_v1_selection_envelope_v1 \
  tests.test_memory_prompt_renderer_v1 \
  tests.test_prompt_assembler_v1 \
  tests.test_resse_runtime_policy \
  tests.test_resse_user_preferences \
  tests.test_resse_decision_bundle
```

Exact result:

```text
Ran 173 tests in 2.448s
OK
```

Focused results:

- Response policy and prompt: 30 passed.
- FM bundle and selector: 30 passed.
- Memory schema, envelope, renderer, and assembler: 90 passed.
- Typed assembler alone: 25 passed.
- Earlier isolated RESSE policy/preference/composite regressions: 22 passed.

### Behavioral evaluations

Server: seebx, isolated worktree

```bash
/opt/chat-memory/venv/bin/python3 scripts/resse_policy_eval.py
/opt/chat-memory/venv/bin/python3 scripts/resse_preference_eval.py
/opt/chat-memory/venv/bin/python3 scripts/resse_composite_eval.py
```

Exact results:

- Policy: 54/54 passed, 35 critical, zero failures.
- Preferences: 32/32 passed, 12 critical, zero failures.
- Composite: 30/30 passed, 20 critical, zero failures.

### Canonical compiler verification

Server: local Mac, canonical source directory

```bash
python3 /tmp/compile_fm_runtime_bundle_v0_2.py \
  --source-dir '/Users/seebx/Documents/Fractal Monism Audit/semantic_model' \
  --output /tmp/fm_v0_2_runtime_bundle.json \
  --check
```

Exact result:

```json
{"bundle_sha256":"a50a256e5d8b84d7521b816054019ab09a4f5a0e4e31b12bf4ecd036f4e002fe","canonical_manifest_sha256":"1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d","output":"/tmp/fm_v0_2_runtime_bundle.json","status":"checked"}
```

### Structural validation

- `git diff --check`: clean.
- `python -m compileall -q rag_engine tests scripts`: passed.
- AST dependency checks: passed; isolated policy, FM, Memory renderer, and assembler modules do not import provider, authentication, database, Qdrant, frontend, or live request-routing code.

### Independent adversarial replay

All tested substitutions failed closed:

- fully rehashed assembled output plus a forged HIGH_STAKES-to-ORDINARY source chain;
- structurally valid and rehashed noncanonical Memory render/application;
- alternate canonical FM record substituted for the deterministic selector output;
- invented and rehashed final Memory context block.

## Trust boundary and Phase 2 requirements

The SHA-256 manifests prove deterministic consistency and tamper reconciliation; they do not prove external provenance. Live integration must construct the following inside trusted backend code and must never accept them from browser fields:

- safety assessment and moderation result;
- response-policy signals;
- Memory selection input and direct-relevance decision;
- FM eligibility and selector request.

Before activation, Phase 2 must:

1. Trace and review the live request path through `app.py`, `rag_engine/vantage_router.py`, `rag_engine/prompt_builder.py`, `rag_engine/persona_loader.py`, `rag_engine/openai_client.py`, and the Verbal Sage chat route.
2. Add one trusted adapter that builds the typed request. Preserve authenticated owner resolution and never make RESSE/FM a Memory owner, extractor, filter, or evidence source.
3. Connect the required server safety assessment to moderation/risk classification, provider safety identifiers, and qualified-human escalation where appropriate.
4. Map system policy, Memory reference data, FM reference data, and conversation to the provider request with deterministic ordering and independent budgets.
5. Replace conservative estimates with exact tokenizer/provider-limit enforcement at the final provider boundary.
6. Bind actual rendered Memory fragments to final answer tracing.
7. Run shadow comparison, safety evaluations, and canary activation before retiring legacy prompt/profile/FM paths.

## Explicit non-changes

Phase 1 did not modify or activate:

- `/opt/chat-memory` production runtime files;
- Verbal Sage frontend files or chat routes;
- `prompt_builder.py`, `vantage_router.py`, `persona_loader.py`, `openai_client.py`, or `app.py`;
- authentication, owner resolution, service-token enforcement, or Supabase logic;
- database schemas, migrations, Qdrant collections, timers, scheduled jobs, or environment variables;
- services, deployments, restarts, or live OpenAI requests.

No private assembled prompt or personal data is included in this handoff. Internal prompt artifacts remain confined to test/runtime objects; this document records only their block structure and sanitized bindings.
