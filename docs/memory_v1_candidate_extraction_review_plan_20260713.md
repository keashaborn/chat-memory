# Memory V1 candidate extraction and review plan — 2026-07-13

## Scope

This phase plans review work over the committed, immutable evidence batch
`15064e5d-8cd3-5611-9cbf-db177d24a3a0`. It does not extract, persist, approve,
promote, retrieve, embed, or inject candidates.

The batch is locked to 164 owner-scoped rows:

| Target lane | Rows | Meaning |
|---|---:|---|
| Governed claims | 122 | Personal facts/events/corrections plus explicitly typed user beliefs |
| Response and life preferences | 18 | How the user wants responses or what the user prefers |
| Project knowledge | 24 | Architecture, requirements, decisions, status, constraints, and roadmap |

Every evidence row produces exactly one deterministic review unit in exactly one
lane. Cross-lane copying is prohibited. A later reviewer may split a unit, but
that creates new hash-locked review artifacts and never changes source evidence.

## Read boundary

`scripts/memory_v1_candidate_review_dry_run.py` connects as `brains_app`, starts
a repeatable-read, read-only transaction, sets `app.user_id` from the locked
manifest, and reads only:

- the authorized batch header;
- its 164 batch ledger rows;
- the corresponding 164 evidence rows;
- existing claim-candidate and preference counts for those evidence IDs;
- database RLS state.

The run fails closed if the batch header, ledger, evidence hash, owner, status,
target, disposition, epistemic role, review flags, sensitivity counts, or
existing target-record counts differ from the manifest. The output contains a
240-character whitespace-normalized preview, never the full evidence content
field.

There is no apply argument, model/API call, embedding request, Qdrant client, or
database mutation statement in this path.

## Lane 1: governed claims

The 122 rows contain 89 user assertions, 30 user beliefs/opinions, and 3 mixed
assertion/belief spans. The output contract is `governed_claim_candidate_v1`.

A reviewed claim draft must specify subject, predicate, object, canonical text,
qualifiers, epistemic role, evidence stance, extraction confidence,
sensitivity, retrieval policy, existing-claim comparison, and evidence IDs.
Extraction confidence measures whether the draft accurately represents the
evidence. It is not truth probability.

User beliefs remain user viewpoints. They do not become external facts. The
reviewer records support, opposition, uncertainty, and provenance rather than a
binary truth declaration. Mixed assertion/belief spans require decomposition.
Approximate dates and quantities retain uncertainty qualifiers. Restricted
health beliefs require restricted-domain review and a surface-policy decision.

The existing `memory.candidate` to reviewed `memory.claim` path can support this
lane after a separately authorized extraction phase.

## Lane 2: response and life preferences

The 18 rows use `user_preference_candidate_v1`. A reviewer must first classify
each as a response preference or life preference, then normalize domain, key,
value, polarity, scope, explicitness, stability, surface policy, extraction
confidence, and evidence IDs.

Background facts and project requirements cannot enter this lane. Existing
`memory.user_preference` is an active-state table, not a reviewed candidate
queue. Before persistence, add a hash-locked preference-candidate and review
workflow. Direct mutation of the active table is not authorized.

## Lane 3: project knowledge

The 24 rows use `project_knowledge_candidate_v1`. A reviewer must provide a
project key, knowledge kind, canonical text, document/decision state, authority
source, effective time, supersession/conflict links, extraction confidence,
sensitivity, and evidence IDs.

Allowed knowledge kinds are architecture, constraint, decision, requirement,
roadmap, and status. Newer text is not presumed better or authoritative. It is
compared with governing documents, prior decisions, and explicit user
ratification.

Project knowledge cannot become a personal claim, preference, or profile card.
No first-class project-knowledge schema currently exists. A reviewed staging
table, durable versioned store, authority model, and supersession model are
required before persistence.

## Review routes

Routes are deterministic and ordered by risk:

1. mixed assertion/belief decomposition;
2. restricted high-stakes domain review;
3. restricted sensitivity review;
4. voice transcription review;
5. subject resolution;
6. canonical rewrite;
7. uncertainty qualification;
8. belief classification;
9. standard reviewed extraction.

A unit can have multiple requirements even though it has one primary route.
Allowed human decisions are accept, rewrite, reject, defer, or split. None of
those decisions promotes a record in this phase.

## Exit conditions

Before semantic extraction begins:

1. jointly review the dry-run summary and special-route units;
2. define the reviewed preference-candidate staging schema;
3. define the project-knowledge staging and durable versioned schema;
4. define target-specific structured extractor schemas and model isolation;
5. define comparison against existing governed claims/preferences/project
   decisions;
6. authorize a separate extraction-only run that still performs zero durable
   writes;
7. review and hash-lock its generated proposals before any persistence design.
