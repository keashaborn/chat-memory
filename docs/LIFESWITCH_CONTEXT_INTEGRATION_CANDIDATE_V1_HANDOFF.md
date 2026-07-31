# LifeSwitch Context Integration Candidate V1

Status: isolated candidate only. Not authorized or ready for production activation.

## Source and scope

- Production base: `8c52472e6de93fa54a1811c36502ed55643f20ad`
- Isolated worktree: `/home/ubuntu/chat-memory-lifeswitch-context-integration-v1`
- Branch: `codex/lifeswitch-context-integration-v1-20260731`
- Replayed design commit: `c8326f7b94309aa3565a1778104a9d4544fa967e`

No production checkout, service, environment variable, database object, Qdrant
collection, authentication boundary, Memory V1 contract, FM/RAG implementation,
or frontend file was changed.

## Authority and data boundaries

LifeSwitch structured data is a separate answer-input lane. It never enters
`MemorySelectionEnvelopeV1`, Memory evidence, governed claims, or Qdrant.
Authentication supplies the owner. V1 is self-owned only; delegated People
access is excluded.

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

`OFF` is evaluated before the restricted LifeSwitch session and guarantees zero
LifeSwitch database reads.

## Versioned candidate path

```text
existing authenticated response-policy plan
  + exact conversation snapshot
  -> LifeSwitchResponseContextProviderV1
  -> restricted repeatable-read, read-only owner transaction
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

The inactive `response_composition_root_v0_3.py` starts only after the current
server-owned safety/mode/Memory/FM/web plan is complete. It does not replace or
weaken that authority path.

## Database candidate

The unapplied SQL candidate creates:

- `lifeswitch_chat_reader_v1`, a no-login, no-bypass-RLS read role;
- `lifeswitch_chat_binding_writer_v1`, a separate no-login append-only role;
- `lifeswitch_chat.account_timezone_v1`, with forced owner RLS;
- narrow column grants that omit raw/free-form notes;
- `lifeswitch_chat.final_answer_lifeswitch_binding_v1`, with forced owner RLS
  and an immutable update/delete trigger.

Reads use one repeatable-read, read-only transaction, a server-established owner
GUC, and `SET LOCAL ROLE lifeswitch_chat_reader_v1`. Missing or invalid timezone
fails date-dependent selection closed. An active-plan timezone is a bounded
fallback until every account has an authoritative account timezone.

The SQL and rollback SQL have not been applied to any database.

## Test evidence

Using `/opt/chat-memory/venv/bin/python` in the isolated worktree:

- Candidate-focused suite: 50 tests passed.
- Existing response-policy, Memory boundary, provider, finalization,
  persistence, and inspector regression suite: 146 tests passed.
- Python compilation passed.
- `git diff --check` passed.

The focused suite verifies:

- `OFF` performs zero LifeSwitch database reads;
- owner/timezone access uses a restricted repeatable-read read-only transaction;
- request, actor, thread, query, and snapshot hashes are bound;
- context ordering and independent token limits;
- LifeSwitch appears as lower-authority named reference data;
- Memory and LifeSwitch create separate final-answer bindings;
- transcript, Memory binding, and LifeSwitch binding persist in one transaction;
- inspector V3 is content-free;
- old V1 contracts and regression tests remain valid.

## Required review before activation

1. Apply the SQL only to a disposable production-schema clone and verify grants,
   forced RLS, role switching, rollback, owner isolation, and query plans.
2. Configure a dedicated restricted read pool without changing existing Memory
   or response-provider pools.
3. Integrate the V0.3 seam into the current production head after a fresh
   shared-file review of composition, routing, finalization, persistence, and
   inspection changes.
4. Run authenticated shadow comparisons with `OFF` read counters and
   content-free traces.
5. Run owner-isolation and date/timezone canaries for nutrition, training,
   measurements, current plan, and combined requests.
6. Confirm separate final-answer Memory and LifeSwitch bindings under rollback
   and replay.
7. Obtain explicit promotion authorization, create a production rollback point,
   and use a gradual canary rollout.

Until those gates pass, do not apply the SQL, wire the live route, deploy,
restart services, or enable production LifeSwitch prompt influence.
