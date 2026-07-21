# RESSE Response Orchestration Phase 3 Handoff

Date: 2026-07-21

Status: isolated integration-readiness implementation complete; production
wiring, persistence, deployment, and canary activation remain unauthorized.

## Repository state

- Server target: seebx
- Isolated server worktree: `/home/ubuntu/chat-memory-resse-response-phase3`
- Server branch: `resse_response_orchestration_phase3`
- Local transfer clone: `/private/tmp/chat-memory-resse-response-phase2`
- Local transfer branch: `resse_response_orchestration_phase3_local`
- Local Phase 2 base: `94001ad`
- Server Phase 2 base: `e2fc38299db4713a820f82034debd967f3ec3297`
- Phase 3 implementation commit in the local transfer clone: `8105a99`
- Final handoff commit: the commit containing this document

The two Phase 2 bases have the same Phase 2 implementation tree but different
handoff commit identities. The Phase 3 implementation is transferred as a
single commit and applied to the isolated server branch only.

## Delivered components

### Backend-owned domain-risk and response-mode classification

`server_response_signal_classifier_v0_2.py` implements the previously pinned
server classifier component instead of allowing a browser value to stand in
for it.

- Explicit acute-risk patterns are evaluated locally before any provider call.
- Broader medical, medication, eating-disorder, mental-crisis, abuse, consent,
  child-safety, withdrawal, legal, financial, and other material-risk cases use
  strict OpenAI Structured Outputs through an injected authenticated client.
- The full user-authority conversation is classified, including short
  continuations that depend on earlier user turns.
- User content is encoded as untrusted JSON below a fixed developer
  instruction. It cannot become classifier policy.
- The provider model is explicit and allowlisted by the future composition
  root; it is not silently substituted.
- Requests use a pseudonymous safety identifier, `store=false`, zero SDK
  retries at this boundary, bounded input/output, and bounded timeout.
- Provider failure, refusal, malformed output, incompatible model identity,
  or inconsistent domain/FM gates fails closed to `UNCERTAIN`.
- Deterministic mode detections cannot be turned off by false model booleans.
- Triggered or uncertain domain risk selects `HIGH_STAKES`, suppresses FM, and
  can require a concrete safety-action closure independently of moderation.

OpenAI moderation remains a separate content-safety layer. Domain risk is not
misrepresented as a moderation result.

### Inactive production composition root

`response_composition_root_v0_2.py` defines the intended authority order:

```text
authenticated actor/thread/request values
  -> owner-scoped repeatable-read conversation snapshot
  -> backend response/domain classifier
  -> trusted signal envelope
  -> independent governed Memory provider
  -> OpenAI moderation
  -> deterministic response policy and FM selection
  -> typed prompt assembly
  -> OpenAI chat provider
  -> central finalization
```

The root imports the real owner-scoped snapshot loader directly. A caller
cannot inject a snapshot, trusted signal envelope, response mode, trusted plan,
or prebuilt OpenAI request. Hidden client values are limited to field names for
legacy diagnostics; their values have no input path.

The module is not imported by `app.py`, `prompt_builder.py`,
`vantage_router.py`, or `persona_loader.py`. It therefore has no live response
influence.

### Central finalization and server-attested assistant output

`response_finalization_v1.py` binds one provider result to:

- the authenticated owner and thread;
- the exact request, conversation snapshot, trusted plan, and provider request;
- one answer ID and provider response;
- content-versus-refusal output kind and output hash;
- the exact injected and answer-model-exposed Memory records and applied
  Memory controls.

It returns a typed assistant transcript attestation and
`FinalAnswerMemoryBindingV1`. It performs no writes. The raw assistant text is
not copied into the attestation. Assistant history remains excluded from the
conversation reader until a separately authorized restricted append-only
writer and attested reader are installed.

### Concrete governed Postgres loaders

`memory_v1_governed_postgres_loaders_v1.py` adds read-only loader boundaries
for claims, scoped project knowledge, and preferences.

- Transactions are `REPEATABLE READ`, read-only, and require effective role
  `brains_app`.
- Claim and project readers verify the pinned database function is stable,
  security-definer, owned by `memory_v5_reader`, and has a fixed search path.
- Owner, thread, requested handle, duplicate-head, and project/component scope
  invariants fail closed.
- Preference reads verify forced RLS across every involved table.
- Loaders return typed-source data and read-control proof only. They never
  return `prompt_block` or `memory_chunks` and report zero database writes.

The loaders deliberately do not invent fields missing from the current read
functions. The existing lane adapters still identify these exact schema gaps:

- `memory.read_v5_shadow_claims(uuid[])` needs `revision_id`,
  `component_key`, `source_content_sha256`, and `superseded_by`.
- `memory.read_v5_shadow_project_knowledge(uuid,integer)` needs `sensitivity`,
  `valid_from`, `valid_to`, `superseded_by`, and `source_rank`.

Closing those gaps requires separately authorized additive database work.

## Evaluation coverage

The new domain-risk evaluation set contains 24 cases, 21 critical:

- acute medical danger;
- medication changes;
- dangerous restriction and eating-disorder context;
- abuse, coercion, child safety, and withdrawal;
- legal and financial decisions;
- mental crisis, sexual safety, and other material risk;
- ordinary, technical, explicit-FM, and coaching false-positive controls;
- technical precedence when FM is the implementation subject;
- high-stakes precedence over explicit FM;
- prompt-injection language;
- a short continuation requiring conversation context.

The focused regression set covers FM selection, governed Memory envelopes and
rendering, lane adapters, prompt assembly, moderation, response policy,
conversation snapshots, orchestration, provider execution, the Phase 2 shadow
wrapper, Phase 3 classification, Postgres loaders, composition, attestation,
and final Memory binding.

## Validation

Local isolated transfer clone:

```text
focused pytest: 245 passed
resse_policy_eval.py: 54/54 passed; 35 critical; 0 failed
resse_preference_eval.py: 32/32 passed; 12 critical; 0 failed
resse_composite_eval.py: 30/30 passed; 20 critical; 0 failed
resse_domain_risk_eval.py: 24/24 passed; 21 critical; 0 failed
python3 -m compileall -q rag_engine tests scripts: pass
git diff --check: pass
```

An unscoped repository-wide pytest discovery is not a valid project command:
it collects archived scripts and unrelated integration suites that require
optional production dependencies. The focused Phase 1-3 suite is the frozen
response-orchestration regression surface.

No live OpenAI generation, moderation, or classifier request was made. Tests
use injected deterministic clients. No credential value was read, displayed,
copied, rotated, or changed.

## Current live request-path map

The live path remains the pre-Phase-3 path:

```text
Verbal Sage /api/chat
  -> seebx /vantage/query
  -> authenticated owner checks
  -> legacy and transitional retrieval/prompt paths
  -> current OpenAI client call
```

The Phase 3 modules remain beside, not inside, that path. Future integration
review must cover these shared files before edits:

- `app.py`
- `rag_engine/prompt_builder.py`
- `rag_engine/vantage_router.py`
- `rag_engine/persona_loader.py`
- `rag_engine/openai_client.py`
- the Verbal Sage frontend chat and inspect routes

Authentication and owner resolution must remain unchanged. `vantage_id=RESSE`
must never become a Memory owner or filter. Memory intent and FM eligibility
must remain independent inputs to final prompt assembly.

## Remaining blockers before live wiring

1. Additive database read-contract changes must expose the missing claim and
   project fields. No migration was authorized in Phase 3.
2. The authoritative governed Memory provider must connect the frozen intent,
   selector, render, and application chain to the composition root.
3. Restricted append-only persistence must store final Memory bindings and
   assistant transcript attestations. Assistant history must remain excluded
   until the attested reader is available.
4. Shared live request files require a joint Memory/response-policy integration
   review and a precise patch plan.
5. The classifier model and request budgets require an explicit production
   configuration decision. They must remain backend-owned.
6. Provider-backed classifier shadow evaluation should run on an allowlisted
   canary before response influence. The deterministic tests do not authorize
   a live provider call.
7. Universal shadow comparison, limited canary activation, legacy prompt-path
   retirement, and rollout require separate authorization.

## Change-control confirmation

Phase 3 did not modify or activate:

- `/opt/chat-memory` production files or branch;
- Verbal Sage frontend files;
- live prompt/request routing or current service behavior;
- authentication, owner resolution, service-token enforcement, or Supabase;
- database objects, migrations, schemas, functions, tables, or RLS;
- Qdrant collections, points, or selection activation;
- environment variables or credentials;
- timers, extraction jobs, promotion jobs, or running services.

Protected assembled prompts, FM source prose, Memory content, and personal data
are not reproduced in this handoff.
