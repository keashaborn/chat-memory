# Memory V1 V5.2 circuit and status contract v1

## Scope

This contract governs private local extraction only. It does not authorize
claim creation, Qdrant projection, retrieval, answer binding, or prompt
influence.

## Outcome handling

Every rejected local extraction receives a sanitized reason code and one
server-owned classification:

- `record_terminal`: the source record is contextual, insufficient,
  non-durable, semantically rejected, sensitive/ambiguous, or requires entity
  or predicate review. It never increments the systemic circuit.
- `systemic_threshold`: transport, provider-contract, persistence, lease, or
  database failures. Consecutive failures increment the circuit.
- `systemic_immediate`: owner/RLS/security or pinned model/runtime/compiler
  mismatch. It opens the circuit immediately and remains operator-latched.
- `neutral`: worker abandonment. It neither opens nor closes the circuit.
- `control`: quota or circuit blocks. It neither increments nor extends the
  circuit.
- Unknown codes fail closed as `systemic_unclassified` and open immediately.

Record-terminal queue dispositions are:

- `skipped`: insufficient evidence, no durable content, ordinary semantic
  rejection, questions, transient state, or structured-domain content.
- `deferred`: ambiguous transcript or unresolved context. The existing queue
  represents this as terminal `skipped` with
  `result.final.payload.disposition=deferred`.
- `review_required`: sensitive/ambiguous content, entity/predicate/project
  resolution, mixed authorship, or compound splitting.

The original completion ledger is never edited. A new append-only outcome event
binds the completion event to its normalized reason and terminal disposition.

## Circuit rules

Circuit identity is the exact tuple:

1. provider ID;
2. provider version;
3. provider model SHA-256;
4. model file SHA-256;
5. runtime revision SHA-256;
6. policy compiler SHA-256.

Rules:

1. The threshold is 10 consecutive `systemic_threshold` outcomes.
2. `systemic_immediate` and unknown failure codes open immediately.
3. An accepted result closes the circuit.
4. A model-called `record_terminal` result closes the circuit because it proves
   transport, model, compiler, validation, and completion paths ran.
5. A record-terminal result produced before a model call does not close an
   existing systemic circuit.
6. Blocked wakeups, quota blocks, and worker abandonment do not increment,
   reset, or extend the circuit.
7. Threshold-open cooldown is 900 seconds from the most recent systemic
   completion.
8. After cooldown, one reservation is allowed in half-open state.
9. While that reservation is incomplete, all other claims are blocked before a
   model call.
10. A successful or record-terminal half-open completion closes the circuit.
11. A systemic half-open completion starts a new cooldown.
12. Immediate security or fingerprint failures remain latched until an
    operator changes the faulty fingerprint/configuration and uses a new exact
    fingerprint or performs an audited recovery.

## Database and API boundary

`memory.owner_v5_local_inference_status_v1(...)` returns owner-scoped aggregate
database state only. It exposes no job IDs, evidence IDs, query text, source
text, packet content, claim prose, or prompt content.

The backend status assembler adds runtime checks for:

- scheduler timer and worker service;
- private tunnel and GPU inference health;
- database connectivity;
- pinned model/runtime/compiler hashes;
- Qdrant reachability and projection consistency.

The public Admin response follows
`MEMORY_V1_V5_LOCAL_STATUS_V1.schema.json`.

State precedence is:

1. `unhealthy` when any mandatory runtime/security check fails;
2. `circuit_blocked`;
3. `quota_limited`;
4. `running` when a lease is active or eligible work is dispatch-ready;
5. `idle_no_eligible_work`.

`activity` distinguishes `active`, `dispatch_ready`, `blocked`, and `none`.
`last_successful_dispatch_at` is the latest durable reservation. The terminal
summary contains only timestamp, accepted/rejected, outcome class, and
sanitized reason code.

## Rollback

Rollback restores the prior claim-function body by exact anchor, drops the
aggregate status and circuit functions, drops the restricted terminal-outcome
function, and drops the classifier. The additive outcome table is dropped only
when empty; if any live outcome event exists, rollback revokes writer access and
retains the table as dormant append-only history. It does not alter preexisting
inference events, packets, queue history, claims, Qdrant, answer bindings, or
prompts. The Python worker change must be rolled back in the same release so it
does not call a removed function.
