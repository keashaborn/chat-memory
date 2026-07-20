# Memory V1 predicate runtime gap audit — V5.1

Date: 2026-07-20

Starting commit: `b4178664fbd14291c966f106a6605f721f805c58`

Scope: seebx Memory V1 extraction and staging only. This audit does not authorize or implement Qdrant writes, governed-claim promotion, retrieval, prompt influence, frontend changes, or external model calls.

## Result

The relationship design is complete enough for runtime integration, but V5.1 is not yet the active extraction contract.

- The base V5 registry contains 25 active predicates.
- The V5.1 composite contains 63 active predicates and 19 legacy-read-only contracts.
- The relationship registry contributes 41 relationship predicates and replaces three base relationship predicates.
- The private Qwen3-14B relationship evaluation passed 60/60 governed cases.
- PostgreSQL contains V5.1 registry metadata, relationship contracts, pattern/salience schema, and shadow pattern rows, but the registry remains `proposed` and `runtime_active=false`.
- The production scheduler invokes `memory_v1_v5_local_inference_canary.py`, which is hash-pinned to the base V5 registry and V5 normalized-packet schema.
- The normalized provider packet model, exact-job canary, staging bundle validators, and `memory.stage_relational_packet_v5` are also pinned to V5.
- No V5.1 observation can currently pass the complete scheduled extraction-to-staging chain.

## Active runtime boundaries

| Component | Current boundary | Required V5.1 change |
|---|---|---|
| `memory_v1_v5_local_inference_scheduler.py` | Invokes canary without a registry/schema profile | Require an explicit hash-bound contract profile and pass it to the canary |
| `memory_v1_v5_local_inference_canary.py` | Hard-coded V5 registry/schema paths and SHA-256 values | Resolve only allowlisted, hash-bound V5 or V5.1 profiles; scheduled V5.1 must be explicit |
| `memory_v1_relational_extraction_v5_provider.py` | Normalized packet and validator emit only V5 contract/registry values | Support the exact V5 and V5.1 contract pairs while preserving V5 replay |
| `memory_v1_relational_extraction_v5_local_provider.py` | Relationship compiler already recognizes V5.1 | Bind it through the normalized provider and add expanded adversarial coverage |
| `memory_v1_v5_local_auto_stage.py` | Calls `memory.stage_relational_packet_v5` | Route by exact packet contract to a new V5.1 staging function |
| `memory_v1_v5_local_entity_validation.py` | Calls V5 staging directly | Route exact V5.1 packets to the V5.1 staging function; reject mixed versions |
| `memory_v1_v5_stage_batch.py` and preflight | Require V5 contract/registry and V5 schema | Add a separate V5.1 batch/preflight contract or a fail-closed profile argument |
| `memory.stage_relational_packet_v5` | Validates and writes only V5 observations | Leave unchanged; add `memory.stage_relational_packet_v5_1` with V5.1 predicate FK checks |
| downstream projection APIs | Several filters require V5 registry | Keep V5.1 in review/staging until each downstream lane has an explicit version-aware contract |

## Compatibility rules

1. Never modify the meaning, hashes, or replay behavior of a persisted V5 packet.
2. V5.1 is a new exact contract pair: `memory_v1_relational_extraction_v5_1` plus `memory_predicate_registry_v5_1`.
3. Registry and schema selection is server-owned. Queue rows, model output, and callers cannot choose arbitrary paths, hashes, or versions.
4. A packet, resolution, stage batch, and database transaction may contain one exact contract pair only.
5. V5.1 observations must reference V5.1 predicate contracts. They must never be silently stored under V5 contracts.
6. Unknown or legacy-read-only V5.1 predicates defer; they are not normalized to a nearby predicate.
7. The model proposal is untrusted. Deterministic policy compilation, entity/temporal normalization, predicate validation, and owner checks run on seebx.
8. V5.1 remains shadow-only until isolation, replay, and bounded private-GPU evaluations pass.

## Implementation sequence

1. Add immutable runtime-profile artifacts binding the V5 and V5.1 registry/schema files and SHA-256 values.
2. Make the normalized provider version-aware for only those two exact pairs and prove unchanged V5 output hashes.
3. Expand relationship and base-predicate confusion, negation, correction, temporal, quotation, question, compound, and third-party tests.
4. Run zero-write private Qwen3-14B evaluation using the V5.1 profile.
5. Add V5.1 PostgreSQL staging/writer functions and rollback-only security tests without changing V5 functions.
6. Route scheduled shadow extraction and auto-stage by the server-owned contract profile.
7. Run bounded owner-only canaries, prove replay and account isolation, then enable nightly V5.1 review staging.
8. Stop before claim promotion, Qdrant, retrieval, or prompt influence.

