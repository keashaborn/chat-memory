# Memory V1 V5 staging and entity batch — 2026-07-17

Server: seebx backend.

Production head after this phase:
`86cb03c23fb69f18d03f324c5601ff9803eb29e6`.

## Source evidence and staging

Two previously reviewed, owner-scoped source records were admitted as canonical
append-only evidence and then staged through the generic V5 batch path. The
target owner was `1240822d-ac9a-4096-95aa-e2b24d36ef50`.

The temporal compatibility migration was production-clone tested before
installation. Its production installation changed no `memory.*` rows. The
complete pre/post memory-table signature was
`89934f057bd1c7f0e4c8eeb11e888126ddfe9756cc7c650b8d6ce9c437a4f49c`.

Pre-stage backup:

- path: `/home/ubuntu/brains/snapshots/memory_pre_v5_temporal_stage_20260717T055337Z_d9596ee.dump`
- SHA-256: `8aa5a9d1c3adac66f15724941c74ee3ddc26b0be6471fac25a00c9b2d22da69e`

Stage apply report:

- path: `/home/ubuntu/memory-v1-reviews/v5-10-v5-13-stage-batch-apply-f621ad9-20260717.json`
- SHA-256: `b00e27befaab4ec181d75819f084b95664de1237e7b01221af628984585063c1`
- exact rows: 94
- bundles: 2 applied, then 2 zero-write replays
- staged rows: 11 mentions, 11 resolution plans, 2 resolution candidates,
  33 observations, 33 temporals, 2 batch rows, and 2 request rows
- external model calls: 0
- Qdrant calls: 0

Only the seven permitted staging tables changed. The non-target-owner staging
signature remained
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

## Generic entity review/apply batch

The reusable runner
`scripts/memory_v1_v5_entity_resolution_batch.py` replaced the prior one-off
resolution scripts for this phase. It accepts a mode-0600 hash-locked manifest,
creates a zero-write plan, requires a short-lived plan-bound authorization,
uses one owner advisory lock and one transaction, and proves review/apply replay
is zero-write.

The production-clone rehearsal applied exactly eight resolutions:

- two trusted self links;
- six manually reviewed named-entity creations;
- 21 observation/entity bindings;
- 61 total append-only rows.

Three role-only resolutions remained deferred: an unspecified place, mother,
and father. The runner did not review or apply them.

Pre-entity backup:

- path: `/home/ubuntu/brains/snapshots/memory_pre_v5_entity_batch_20260717T061847Z_86cb03c.dump`
- SHA-256: `946d4725cf0d3266178ed25cf436492b7500e6bcbe94eb163c3733a7c63159a0`

Entity apply report:

- path: `/home/ubuntu/memory-v1-reviews/v5-10-v5-13-entity-resolution-batch-apply-86cb03c-20260717.json`
- SHA-256: `3d6e377ad201f05445d45153572f2210781e7d54f1e35b1898252fb1a194400f`
- exact rows: 61
- resolutions: 8 applied, then 8 zero-write replays
- reviews: 6
- new durable entities: 6
- new alias observations: 6
- new resolution applies: 8
- new observation bindings: 21
- new operation requests: 14
- external model calls: 0
- Qdrant calls: 0

The non-target-owner signature across every entity-batch mutable table remained
`ebeddf62f83b7fcfa1a175f48470ae37792079fb47fab3efd46d41d1a6e2f8b8`.
The full `memory_claim_v1` Qdrant signature remained
`750a2488ada87e60faf14100559b7010511f27abc118e1121a4cf358c2bc2a91`.
Brains remained active and authenticated `/healthz` returned `status=ok`.

## Projection boundary discovered

The 21 bound observations were classified without source chat text or prompt
content:

- 15 are eligible for governed claim projection;
- five `identity.name` observations are already represented by the durable
  entity plus alias-observation path and should not become redundant claims;
- one former-pet relationship is deferred because its extracted open temporal
  interval conflicts with the same record's death event.

The installed `memory.preflight_projection_packet_v5` API is intentionally an
occupation-only canary. It requires exactly one projection, projector version
`occupation_claim_v1`, predicate `occupation.works_as`, one entity object, and
fixed canonical prose. It cannot safely validate these 15 projections.

The next phase is therefore an additive generic projection preflight contract
and production-clone suite. It must support governed registry predicates,
literal and entity objects, deterministic canonical rendering, multi-item row
budgets, identity-observation suppression, temporal-conflict deferral, and
manual review for sensitive predicates. It must stop before Qdrant, retrieval,
or prompt influence.
