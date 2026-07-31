# LifeSwitch Context Integration Candidate V1

Status: corrected isolated candidate. Ready for Memory/security re-review, not
authorized for production activation.

## Source and scope

- Production base: `90b1f8e92709074db1c62536e0004ac9106342ce`
- Isolated worktree: `/home/ubuntu/chat-memory-lifeswitch-context-gateway-v1-current`
- Branch: `codex/lifeswitch-context-gateway-v1-current-20260731`
- Rebased LifeSwitch integration parent: `c94eccf`
- Owner-gateway correction commit: `86e2cf0ddb9adfc82de905822de737ab048f9ea7`

No production checkout, service, environment variable, database object, Qdrant
collection, authentication boundary, Memory V1 contract, FM/RAG implementation,
or frontend file was changed.

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

## Versioned runtime candidate

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

The inactive `response_composition_root_v0_3.py` begins only after the current
server-owned safety, mode, Memory, FM, and web plan is complete. It does not
replace or weaken that authority path.

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

Using PostgreSQL 16 and `/opt/chat-memory/venv/bin/python`:

- Candidate-focused suite: 54 tests passed.
- Existing response-policy, Memory boundary, provider, finalization,
  persistence, and inspector regression suite: 250 tests passed.
- Python compilation passed.
- `git diff --check` passed.
- SQL apply and rollback passed in a disposable PostgreSQL 16 container.
- Rollback left the candidate schema and both candidate roles absent.
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

1. Memory/security review must return `LIFESWITCH_CONTEXT_INTEGRATION_READY: YES`
   for this corrected candidate.
2. Recheck the then-current production head and shared-file overlap.
3. Configure a dedicated restricted pool without changing existing Memory or
   response-provider pools.
4. Integrate the V0.3 seam only after joint shared-file review.
5. Run authenticated shadow comparisons with `OFF` read counters and
   content-free traces.
6. Run owner-isolation and date/timezone canaries for every projection.
7. Confirm separate Memory and LifeSwitch answer bindings under rollback and
   replay.
8. Obtain explicit promotion authorization and use a gradual canary rollout.

Until those gates pass, do not apply the SQL, wire the live route, deploy,
restart services, or enable production LifeSwitch prompt influence.
