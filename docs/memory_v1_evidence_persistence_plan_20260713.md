# Memory V1 reviewed-span evidence persistence plan

Status: dry-run design only. No apply path is authorized or implemented.

## Scope

Persist only final review-bearing spans from the 38 split-required authenticated
`frontend/chat:user` turns:

- Atomic spans with `review_claim_span`, `review_belief_span`,
  `review_preference_span`, or `review_project_span`.
- Review-bearing compound children that replace every
  `manual_split_required` atomic parent.

Do not duplicate questions, quoted/pasted text, context-only spans, temporary
state, structured adapter operations, or full chat turns into
`memory.evidence`. Those remain available in their authoritative source stores.
This phase does not include the two compact project-review turns outside the
atomic/compound set.

## Row identity and hash lock

Each row is identified by:

```text
owner_user_id = authenticated Supabase UUID
source_system = public.chat_log
external_id   = chat_log:<source_uuid>:span:<stable_span_uuid>
evidence_id   = UUID5(owner_user_id, source_system, external_id)
```

The source UUID, full-source SHA-256, absolute offsets, stable span UUID, and
span SHA-256 must all reproduce the exact live source text. Any mismatch fails
closed. `vantage_id` is retained only as capture provenance; it never owns,
filters, partitions, or aliases memory.

## Planned values

- `kind=user_statement`
- `directness=1.0`
- `source_reliability=NULL`; user testimony is evidence, not automatic truth.
- `sensitivity=high`; spans explicitly flagged for sensitivity review are
  `restricted`.
- `status=active`
- `independence_key=user_statement_sha256:<span_sha256>` so exact repetition is
  preserved as separate provenance but not counted as independent
  corroboration.
- Metadata fixes source provenance, offsets, lane, disposition, review flags,
  and explicitly disables retrieval, prompt use, and candidate creation.

Belief spans remain labeled `belief_not_external_fact` with epistemic role
`user_belief_or_opinion`. Factual-looking personal statements remain
`user_assertion`, not verified truth. Evidence persistence does not convert a
belief, opinion, preference, project statement, or biography into an approved
claim.

The final review layer conservatively adds metadata without changing source
text: health or medication beliefs require high-stakes review and are
`restricted`; approximate numeric autobiography requires an uncertainty
qualifier; and inseparable event/opinion clauses use
`mixed_user_assertion_and_belief`. These flags govern later extraction only.

## Replay and deduplication

The future apply algorithm, if separately authorized, is:

1. Start a short transaction and set transaction-local `app.user_id`.
2. Rebuild rows from the authoritative source and recheck every hash and offset.
3. `INSERT ... ON CONFLICT DO NOTHING`; never update an evidence row.
4. Select the row under forced owner RLS.
5. Compare every immutable field, including deterministic `evidence_id`, exact
   content, hashes, timestamp, independence key, sensitivity, and metadata.
6. Reuse only an exact row. Roll back the entire batch on any mismatch.

Same-content occurrences with different source identities remain separate rows
for provenance but share the same independence key. Existing full-turn evidence
is reported as covering provenance; it does not replace an exact span row and is
not independent corroboration.

## Security boundary

`memory.evidence` has forced RLS using transaction-local `app.user_id` and a
unique `(owner_user_id, source_system, external_id)` identity. The live
`brains_app` role also has `UPDATE` and `DELETE`, so append-only behavior is not
currently enforced by database privilege. Before any apply path exists, use a
narrow insert/select-only persistence boundary or an equally strict database
function and add an audited redaction/deletion mechanism separately.

The dry-run command opens read-only transactions and has no apply flag, evidence
insert, candidate insert, claim promotion, preference mutation, Qdrant write, or
prompt integration.
