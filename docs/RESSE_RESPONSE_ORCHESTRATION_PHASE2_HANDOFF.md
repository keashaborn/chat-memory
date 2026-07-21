# RESSE Response Orchestration Phase 2 Handoff

Date: 2026-07-20

Status: isolated implementation complete; live wiring is not authorized and is not ready.

## Repository state

- Server: seebx
- Worktree: `/home/ubuntu/chat-memory-resse-response-phase2`
- Branch: `resse_response_orchestration_phase2`
- Production base: `55bde3d820d3fbe61b11aed4120c12323b3fa05f`
- Phase 1 merge: `89d1eaafeeebe9d39cb44daef81eea19a565d29d`
- Phase 2 implementation on seebx: `0e08ebc2ff3a58a36b9a7a5a4128512367d74fbe`
- This handoff is committed immediately after the Phase 2 implementation.

The live checkout `/opt/chat-memory` remained clean and unchanged at
`55bde3d820d3fbe61b11aed4120c12323b3fa05f` throughout this work.

## Delivered components

### Response trust boundary

- `response_conversation_snapshot_v1.py` loads an owner/thread/request-bound,
  repeatable-read transcript snapshot.
- Legacy `chat_log.source` is treated as request-authored. Only user-authority
  history is admitted. Assistant history is withheld until server-attested
  assistant capture exists.
- A missing current request row produces a current-turn-only snapshot.
- `TrustedPolicySignalsEnvelopeV0_2` binds backend policy signals to the exact
  conversation snapshot and a fixed classifier component version.
- `TrustedResponseRequestV0_2` accepts a validated snapshot, signal envelope,
  and optional governed Memory assembly input. It has no browser-controlled FM
  lens, assistant identity, or hidden prompt-value input.

### Safety and response selection

- `openai_moderation_adapter_v0_2.py` evaluates every provider-visible user
  turn with `omni-moderation-latest` through an injected OpenAI client.
- Moderation failures, malformed results, incomplete category surfaces, and
  input-limit violations fail closed to an `UNCERTAIN` safety gate.
- The adapter disables SDK retries at this boundary and uses bounded timeouts.
- Safety assessments bind the exact request, conversation, ordered assessed
  user-message hashes, and assessor component.
- The orchestrator accepts only the pinned moderation assessor bundle before
  applying the deterministic response policy.
- `HIGH_STAKES` and `TECHNICAL` suppress FM. `FM_EXPLICIT`, `COACHING`, and
  `ORDINARY` use the canonical policy and FM selection contracts from Phase 1.

### FM, Memory, and prompt assembly

- FM v0.2 remains a separate reference-data block selected by response policy.
  It is not a memory owner, extractor, evidence source, or personal-memory
  filter.
- The prompt assembler preserves deterministic authority ordering and separate
  Memory/FM budgets.
- FM opt-out or an application veto can turn FM off even when the semantic mode
  is `FM_EXPLICIT`.
- Governed lane adapters were added for claim, project-knowledge, and life-
  preference sources. They reject legacy rendered prompt blocks, cross-owner
  rows, unpinned source versions, missing provenance, invalid temporal state,
  sensitivity violations, and budget overflow.
- Qdrant is candidate-ID discovery only. Postgres remains content authority.
- Response-style preference controls remain closed until a trusted direct-
  relevance/entity-scope contract exists.

### Provider boundary and shadow evaluation

- `OpenAIChatCompletionsAdapterV1` accepts only a validated
  `TrustedResponsePlanV0_2`; it constructs the provider request internally.
  A prebuilt or self-rehashed request DTO cannot be executed.
- The provider call uses the existing injected OpenAI client, `store=false`, a
  bounded completion budget, zero SDK retries at this boundary, and a stable
  pseudonymous safety identifier.
- Provider output fails closed on a mismatched model, invalid dated model
  snapshot, nonzero or Boolean choice index, non-assistant role, non-`stop`
  finish state, tools/functions/audio, missing or ambiguous output, whitespace-
  only output, invalid usage, or a blank response ID.
- Shadow runtime is default-off, allowlist-gated, performs no generation or
  governed Memory selection, and cannot influence the live response.
- Exportable shadow and snapshot reports contain operational outcomes and
  counts only. Content-derived and Memory/prompt-derived hashes remain private
  typed-plan integrity fields rather than public telemetry.

## Validation

Local isolated worktree:

```text
243 passed in 1.29s
python3 -m compileall -q rag_engine tests: pass
git diff --check: pass
```

seebx isolated worktree using `/opt/chat-memory/venv/bin/python3`:

```text
unittest: 228 tests, OK
resse_policy_eval.py: 54/54 passed; 35 critical; 0 failed
resse_preference_eval.py: 32/32 passed; 12 critical; 0 failed
resse_composite_eval.py: 30/30 passed; 20 critical; 0 failed
python3 -m compileall -q rag_engine tests: pass
git diff --check: pass
```

The production virtualenv does not contain `pytest`. No dependency was
installed or changed. The 15 pytest-style governed-lane adapter tests are part
of the 243-test local result; the remaining 228 tests ran unchanged in the
server's production Python environment.

No live OpenAI generation or moderation request was made. The existing OpenAI
credential was not read, displayed, copied, rotated, or modified. The new
adapters accept the server's existing authenticated client by dependency
injection for future authorized integration.

## Required work before live wiring

1. Add and evaluate a governed, high-recall domain-risk classifier for broader
   medical, medication, eating-disorder, legal, financial, coercion, and other
   material-risk requests. OpenAI moderation plus bounded deterministic rules
   is not sufficient as the sole production mode selector.
2. Build one production composition root that guarantees:
   - conversation snapshots come only from the authenticated owner-scoped DB
     loader, never request JSON or test helpers;
   - policy-signal envelopes come only from the backend classifier, never
     browser fields;
   - the concrete safety provider is `OpenAIModerationAdapterV0_2`;
   - trusted-plan and OpenAI-request wire models are never client inputs.
3. Create server-attested assistant transcript capture before assistant history
   is eligible for provider-visible context.
4. Implement and clone-test concrete governed Memory lane loaders. Current V5
   source functions still omit fields required by the frozen typed envelopes;
   the adapters intentionally report exact schema gaps instead of falling back.
5. Connect central final-answer Memory binding and append-only restricted audit
   ledgers before governed selections influence answers.
6. Trace and review the final shared request path before modifying
   `prompt_builder.py`, `vantage_router.py`, `persona_loader.py`, provider client
   construction, or the Verbal Sage chat route.
7. Run universal shadow comparison and adversarial safety/domain evaluations,
   then authorize a canary separately. Do not activate from this branch.

## Protected semantic inputs

Canonical FM v0.2 artifacts remain in the protected local Fractal Monism Audit
semantic-model directory. They were consumed through the previously compiled,
hash-verified runtime bundle. This handoff does not reproduce private assembled
prompts, source prose, personal data, or Memory content.

## Change-control confirmation

This phase did not modify or activate:

- `/opt/chat-memory` production files or branch;
- Verbal Sage frontend files;
- live prompt/request routing;
- authentication, owner resolution, service-token enforcement, or Supabase;
- Memory V1 database objects, schemas, extraction, promotion, timers, or jobs;
- Qdrant collections or points;
- environment variables or credentials;
- running services.
