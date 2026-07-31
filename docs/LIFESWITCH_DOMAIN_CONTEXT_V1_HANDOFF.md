# LifeSwitch Domain Context V1 Candidate Handoff

Status: isolated implementation candidate; not connected to production chat

## Candidate location

- seebx worktree: `/home/ubuntu/chat-memory-lifeswitch-domain-context-v1`
- branch: `codex/lifeswitch-domain-context-v1-20260731`
- base production commit: `ee9ad5501ab3ff53f55ce0c554915a4f03f94571`
- production files, services, databases, Qdrant, Memory V1, FM, RAG, and frontend: unchanged

## What the candidate provides

The candidate establishes a separate typed lane for current structured
LifeSwitch data. It does not treat plan, nutrition, training, conditioning, or
measurement data as personal Memory V1 and does not use Qdrant.

`LifeSwitchDataPlanV1` makes a bounded, server-side decision from the current
message. Clear personal requests select one typed projection. Ambiguous,
general, and unrelated requests fail closed to `OFF` and perform no LifeSwitch
read.

`TrustedLifeSwitchContextRequestV1` binds the plan to a request, authenticated
actor, self-owned owner UUID, trusted IANA timezone, query hash, and request
hash. V1 deliberately excludes delegated People access.

`LifeSwitchDomainContextProviderV1` dispatches exactly one projection through
a source allowlist. The Postgres reader uses parameterized, owner-scoped,
read-only queries and existing canonical plan-observation logic.

`LifeSwitchDomainContextEnvelopeV1` returns bounded typed sections, source
provenance, plan-source status, content hashes, dates, record counts, and a
compact reference block. Raw UUIDs and the original query are not rendered.

## Implemented projections

- current agentic plan, with explicit legacy-plan fallback only when no active
  agentic plan exists;
- one-day and bounded-range nutrition with calories, protein, carbohydrate,
  and fat;
- bounded resistance-training and conditioning summaries;
- one-day resistance and conditioning sessions without free-form notes;
- named-exercise progression;
- bounded measurement trends;
- compact combined plan-versus-actual status.

The exact request-to-source map, windows, budgets, and authority rules are in
`docs/LIFESWITCH_CHAT_DATA_SOURCE_MAP_V1.md`.

## Verification evidence

Focused candidate tests:

```text
23 tests passed
```

Existing plan, observation, progression, response-policy, and prompt-assembly
regressions:

```text
81 tests passed
```

Live Postgres shadow verification ran inside a read-only transaction against
one internally selected active-plan owner. It emitted no UUID or personal
values and performed no writes:

```text
OFF                   0 records   0 estimated tokens
OVERALL_STATUS       42 records 751 estimated tokens
NUTRITION_DAY         1 record  250 estimated tokens
EXERCISE_PROGRESSION  0 records  78 estimated tokens, explicit partial result
TRAINING_SESSION      1 record  113 estimated tokens
writes_performed=False
```

The unrelated entertainment question took the `OFF` path and executed no
LifeSwitch reader call.

## Required production-integration work

1. Establish one trusted account-timezone source on the authenticated response
   request. Do not accept a browser-supplied timezone as historical-date
   authority without server validation.
2. Add a versioned LifeSwitch contribution to typed prompt assembly. Do not
   reuse Memory, FM, prior-web, or generic caller-context slots.
3. Keep `response_mode`, `memory_intent`, `search_intent`, FM eligibility, and
   `LifeSwitchDataPlanV1` independent. Safety policy controls how selected data
   may be used; it does not grant data access.
4. Bind the exact rendered LifeSwitch context hash and contract version to the
   final answer trace.
5. Expose only content-free Inspector fields: intent, domains, date window,
   plan-source label, counts, token estimate, contract version, and hash.
6. Run authenticated end-to-end cases for unrelated, nutrition-day, training,
   measurements, overall-status, missing-data, wrong-owner, and high-stakes
   requests before activation.
7. Activate behind a backend-owned canary flag with a one-change rollback.

## Known limits

- The deterministic V1 planner intentionally favors false negatives over
  unintended private-data reads. A future trusted classifier may propose typed
  intent signals but may never choose the owner, table, SQL, or budget.
- Exercise matching is bounded text matching; canonical exercise identifiers
  and aliases should replace it when that contract is ready.
- Some Analyze-page calculations remain browser-side. Chat and UI should later
  consume one backend analysis contract rather than duplicate arithmetic.
- LifeSwitch tables currently rely on service-role and backend owner binding
  rather than table RLS. RLS hardening requires a separate reviewed migration.
- No Memory V1, FM corpus, web retrieval, frontend, chat route, prompt builder,
  or response-trace wiring is included in this candidate.

## Safe rollback

Because no production integration exists, rollback is deletion of the isolated
worktree and branch. A later live integration must have its own exact-head
preflight, backup/rollback manifest, canary, and post-deployment verification.
