# Memory V1 V5.2 corrected-packet admission plan V1

## Purpose

This candidate reconciles an authoritative replacement extraction packet with
already-staged relational observations. It prevents corrected packets from
creating duplicate entities or blindly duplicating observations.

The candidate is planning-only. It can persist an immutable proposal, but it
does not create or modify entities, observations, claims, projections, Qdrant
points, retrieval state, or prompt influence.

## Generic decisions

Each entity mention is classified as one of:

- `reuse_existing_resolution`
- `manual_entity_resolution_required`
- `manual_ambiguous_entity_resolution`

Each observation receives two independent decisions.

Storage action:

- `admit_new_observation`
- `repair_existing_provenance`
- `reuse_existing_observation`
- `blocked_entity_resolution`
- `manual_duplicate_observation`

Governance action:

- `controlled_review_eligible`
- `manual_sensitive_review`
- `hold_possible_temporal_update`

This separation prevents a valid provenance repair from silently resolving a
semantic or temporal conflict.

## Exact two-record clone result

The stance packet reuses the existing self resolution and proposes one
provenance repair. Its source explicitly supports the reported stance; the
prior durable observation points at the wrong source span.

The father packet reuses the existing reviewed father resolution instead of
creating another person. It classifies:

- identity name: admit new, controlled review eligible;
- assisted-living care setting: admit new, sensitive manual review;
- severe dementia: reuse the existing observation, sensitive manual review;
- approximately three-second memory: repair provenance and retain a temporal
  update hold against the earlier approximately 30-second report.

Clone totals:

- two existing entity resolutions reused;
- two new observations proposed;
- two provenance repairs proposed;
- one existing observation reused;
- one temporal update held;
- zero blocked atoms;
- zero claim rebindings required;
- zero model calls;
- zero production, Qdrant, retrieval, or prompt writes.

## Verified controls

- Authenticated owner context is mandatory.
- A packet must be the current owner-authoritative supersession leaf.
- A manual-review route must exist and a terminal-no-stage route must not.
- Cross-owner packet access fails closed.
- Proposals are append-only and hash-bound.
- Proposal replay writes zero rows.
- Rollback restores the prior ACL signature exactly.

## Next boundary

The next candidate should add reviewed apply semantics without changing the
planner:

1. Create successor observations for provenance repairs.
2. Link each successor to its prior observation and replacement packet.
3. Reuse the prior mention, resolution, and entity when the plan says so.
4. Admit only observations classified `admit_new_observation`.
5. Record exact reuse outcomes without duplicating observations.
6. Keep `hold_possible_temporal_update` out of entailment and claim creation.
7. Prove zero-write replay, forced-RLS isolation, and no downstream influence.

Production installation and durable correction/application are not part of
this candidate.
