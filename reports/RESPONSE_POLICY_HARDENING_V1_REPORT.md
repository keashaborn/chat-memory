# Response Policy Hardening V1 Candidate Report

Date: 2026-07-30
Server: seebx
Production base at candidate creation:
`fbfc78763653659340398c88c242aafa872fd9b9`
Isolated worktree:
`/home/ubuntu/chat-memory-response-policy-hardening-v1`
Branch: `codex/response-policy-hardening-v1-20260730`

## Result

The isolated candidate closes every reviewed regression in the 52-case policy
catalog. It does not alter production and is not authorized for deployment.

## Runtime corrections

1. Local hard safety rules now require narrow personal or immediate evidence.
   General educational vocabulary reaches contextual classification.
2. Provider false positives in clearly general educational contexts are
   calibrated without masking personal danger or required safety action.
3. Ordinary fitness and nutrition vocabulary remains separate from actual
   restriction, purging, fainting, starvation, or eating-disorder evidence.
4. Exact guided-reflection requests survive provider unavailability while FM
   and behavioral intervention remain disabled.
5. Exact direct requests cannot drift into material clarification.
6. Technical explanations cannot drift into interactive procedure. Simple
   arithmetic cannot drift into technical or FM mode.
7. Intervention-design intent is separate from consent to a specific
   experiment. Explicit consent negation closes every `FM-IR-020` prerequisite.
8. General non-FM education cannot acquire light FM framing solely from
   provider drift.

## Verification

### Deterministic policy matrix

- Cases: 52
- Passed: 52
- Failed: 0
- Unexpected failures: 0

### Focused policy and inspection suite

- Tests: 123
- Passed: 123
- Failed: 0

### Compatibility suite

The orchestration, prompt assembly, composition root, OpenAI provider, and FM
selection compatibility modules were run together.

- Tests: 106
- Passed: 106
- Failed: 0

### Live provider evaluation

All calls used synthetic inputs, `store=false`, the already-authorized service
credential held only in process memory, and no product route or database.

Current classifier model `gpt-5.1`:

- Cases: 35
- Repeats: 2
- Provider runs: 70
- Passed: 70
- Failed: 0
- Unstable case IDs: none

Comparison model `gpt-5.6-sol`:

- Targeted provider-sensitive cases: 5
- Repeats: 2
- Provider runs: 10
- Passed: 10
- Failed: 0
- Unstable case IDs: none

Live output files remain under `/tmp` and are intentionally uncommitted.

## Scope confirmation

No production checkout, service, route, authentication rule, environment
variable, database object, Memory V1 object, Qdrant collection, FM corpus/RAG
component, web-search path, voice path, or frontend file was changed.

The candidate is ready for independent diff review and production-integration
preflight. Promotion remains a separate authorization.
