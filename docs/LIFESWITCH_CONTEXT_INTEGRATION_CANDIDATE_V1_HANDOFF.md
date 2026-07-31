# LifeSwitch Context Integration Candidate V1

Status: current-production isolated integration candidate. Memory/security
review is green. All candidate gates pass; production activation remains
separately approval-gated.

## Source and scope

- Production base: `fa0bb50227fa4a35f8da973197ea2891b0476163`
- Backend worktree: `/home/ubuntu/chat-memory-lifeswitch-context-integration-current`
- Backend branch: `codex/lifeswitch-context-integration-current-20260731`
- Replayed contract commits: `fcf9f13`, `eeb5da9`, `41a2042`, `ffe912c`
- Backend integration commit: `b13471ff051b3b28897d0deb7106fcaccd50cffa`
- Frontend base: `ed5d05a18ca31b3f224810ddb23ee7cda1d58354`
- Frontend worktree: `/tmp/verbalsage-lifeswitch-context-integration-current`
- Frontend branch: `codex/lifeswitch-context-integration-current-20260731`
- Inspector compatibility commit: `676dd227d20c7200bae315ebae77ce11a6d227ae`

No production checkout, service, environment variable, database object, Qdrant
collection, authentication boundary, Memory V1 contract, FM/RAG implementation,
or live frontend file was changed. All work remains in isolated worktrees.

## Authority and data boundaries

LifeSwitch structured data remains a separate answer-input lane. It never enters
`MemorySelectionEnvelopeV1`, Memory evidence, governed claims, or Qdrant.
Authentication supplies the owner. V1 is self-owned only; delegated People
access remains excluded.

The deterministic context order is:

1. `governed_memory_v1`
2. `lifeswitch_domain_context_v1`
3. `fractal_monism_v0_2`
4. `prior_web_provenance_v1`

Independent budgets remain enforced:

- Memory: existing 1,200-token maximum
- LifeSwitch: 1,000 tokens
- FM: existing mode-dependent maximum, up to 1,600 tokens
- Web provenance: 16,384 bytes and 4,096 tokens
- Combined prompt: 32,000-token hard input maximum

`OFF` is evaluated before a database session is acquired and guarantees zero
LifeSwitch database reads.

## Versioned runtime integration candidate

```text
existing authenticated response-policy plan
  + exact conversation snapshot
  -> LifeSwitchResponseContextProviderV1
  -> trusted owner-context binder
  -> restricted repeatable-read, read-only gateway transaction
  -> LifeSwitchPreparedContextV1
  -> TrustedLifeSwitchResponsePlanV1
  -> AssembledPromptV2
  -> OpenAIChatRequestV3 / OpenAIChatResponseV3
  -> FinalizedTrustedResponseV2
       - existing FinalAnswerMemoryBindingV1, unchanged
       - separate FinalAnswerLifeSwitchBindingV1
  -> atomic ResponsePersistenceV2
  -> content-free response_inspection_v3
```

The V0.3 root begins only after the current server-owned safety, mode, Memory,
FM, and web plan is complete. It does not replace or weaken that authority
path. A backend-owned `off|canary|on` gate defaults to `off`; canary owners come
only from server configuration. `OFF` follows the exact V0.2 runtime and creates
no LifeSwitch pool or database read.

Active owners use a separate lazy, bounded connection pool configured only by
`LIFESWITCH_CHAT_POSTGRES_DSN`. The pool verifies the `brains_app` session
identity and rejects superuser or `BYPASSRLS` connections before use. Reads then
switch locally to the restricted no-login reader inside the reviewed gateway
transaction. The new path builds one trusted base plan and makes one final
answer-model call; it does not generate a discarded V0.2 answer first.

The separate frontend candidate accepts `response_inspection_v3`, labels the
runtime `resse_response_v0_3`, and displays only content-free LifeSwitch status,
record/token counts, projection names, and final-answer binding. V1/V2 trace
rollback compatibility remains intact.

## Corrected database boundary

The unapplied SQL candidate now creates a restricted gateway rather than giving
the reader direct source-table access.

- `lifeswitch_chat_reader_v1` is no-login, no-inherit, and no-bypass-RLS.
- All source-schema and source-table privileges are explicitly revoked from it.
- `brains_app` creates a short-lived opaque context only when authenticated
  `app.user_id`, `app.lifeswitch_owner_id`, and the requested owner match.
- The context is bound to the PostgreSQL backend PID, owner, thread, request
  hash, conversation snapshot hash, and a five-minute expiry.
- Gateway functions accept the opaque context UUID, never an owner UUID.
- Every gateway is `SECURITY DEFINER` with an empty fixed `search_path` and
  restricted `EXECUTE` privileges.
- LifeSwitch reads occur in a repeatable-read, read-only transaction after
  `SET LOCAL ROLE lifeswitch_chat_reader_v1`.
- Plan output is recursively reduced to explicit target fields. `coach_notes`,
  `body_state`, `monitoring_rules`, arbitrary nested notes, and the complete
  source JSON document cannot reach the reader.
- The owner context is removed after the read and expires closed if cleanup is
  interrupted.
- The answer-binding writer remains a separate no-login append-only role with
  forced owner RLS.

Missing or invalid timezone still fails date-dependent selection closed. The
active-plan timezone remains a bounded fallback until every account has an
authoritative account timezone.

## Validation evidence

Using PostgreSQL 16, the production Python environment, and an isolated Next.js
build:

- LifeSwitch-focused backend suite: 60 tests passed.
- Existing response-policy, Memory boundary, provider, finalization,
  persistence, usage, and inspector regression suite: 261 tests passed.
- Current Memory packet-router regression suite: 14 tests passed.
- Total backend tests in the final matrix: 335 passed.
- Frontend trace tests: 11 passed.
- Isolated Next.js production build with TypeScript validation: passed, 70/70
  static pages generated.
- Python compilation passed.
- `git diff --check` passed.
- SQL apply and rollback passed in a disposable PostgreSQL 16 container.
- Rollback left the candidate schema and both candidate roles absent.
- The corrected SQL also applied and rolled back cleanly against a disposable
  clone of the complete current production schema; all eight gateway readers
  were present during the apply.
- The real restricted runtime canary proved an unrelated query performed zero
  pool calls and zero database reads, while a current-plan query made one
  dedicated-pool call and returned four bounded records.
- Production metadata showed zero `PUBLIC` table grants across the five source
  schemas used by the gateway.
- The trusted `sage` function owner has `SELECT` on all twelve whitelisted
  source relations; no broader reader grants are needed.

The disposable two-owner malicious-access test passed all ten gates:

1. Authenticated owner mismatch cannot create a context.
2. Changing owner GUCs after binding cannot redirect the context.
3. Private top-level and nested plan fields do not cross the whitelist.
4. Every typed gateway projection returns only owner A records.
5. Direct SQL reads of every underlying source relation are denied.
6. A random context UUID is denied.
7. A valid context is denied from another PostgreSQL backend PID.
8. A separately bound owner B receives only owner B records.
9. An ended context is denied.
10. Apply/rollback leaves no gateway schema or restricted roles behind.

## Review and activation gates

1. Recheck both then-current production heads and changed-file overlap.
2. Review the exact backend and frontend commit ranges above.
3. Prepare a rollback manifest and production-schema backup.
4. Apply the reviewed SQL through the production migration procedure and prove
   direct-table denial, owner isolation, timezone behavior, and all eight
   projections before enabling any owner.
5. Configure the dedicated DSN and start with backend mode `off`; prove the
   existing V0.2 route remains exact and performs zero LifeSwitch reads.
6. Promote the frontend V3 Inspector compatibility before exposing a V3 canary
   trace.
7. Enable one explicitly authorized owner in `canary` mode; verify separate
   Memory and LifeSwitch answer bindings and content-free inspection.
8. Expand gradually only after OFF, owner-isolation, timezone, every-projection,
   rollback, and final-answer canaries pass.
9. Obtain explicit production promotion and canary authorization.

Until those gates and approvals pass, do not apply the SQL, deploy either
candidate, restart services, change environment variables, or enable production
LifeSwitch prompt influence.
