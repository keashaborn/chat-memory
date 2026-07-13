# Memory V1 artifact archive cutover — 2026-07-13

## Scope

Production seebx received the governed artifact schema and exactly five hash-locked,
archive-only artifacts. No artifact section was made eligible for retrieval, claim
promotion, prompt injection, or Qdrant projection.

## Backup

- Path: `/home/ubuntu/memory_pre_artifact_ingest_20260713T123249Z.dump`
- Format: PostgreSQL custom archive, full `memory` schema, data included
- Size: 78,305 bytes
- SHA-256: `eb479a95c90eb0fad97bf7195b9d2355953e56b1ca0b706378df69761eee35a5`
- Restore catalog: `/home/ubuntu/memory_pre_artifact_ingest_20260713T123249Z.dump.list`
- Restore catalog entries: 142

## Production commits

- `0780370 memory: add governed artifact provenance schema`
- `2075273 memory: add hash-locked artifact ingestion dry run`
- `d55147b memory: guard archive-only artifact cutover`

## Manifest and report

- Manifest: `ops/manifests/memory_v1_artifacts_20260713.json`
- Manifest SHA-256: `f1f97c5a172f0c33ed2655233932b489dd9fb2156070d176da0b3cd9534fb856`
- Dry-run report SHA-256: `dbbfc9d0322e83efa32977573fe626faa0c51a674117b826355f03ef5460b8e3`
- Validated source characters: 90,517
- Deterministic sections: 213

## Pre-change production counts

- entity: 1
- evidence: 4
- claim: 6
- candidate: 7
- projection_outbox: 6
- artifact tables: not installed

## Post-change production counts

- entity: 1
- evidence: 9
- claim: 6
- candidate: 7
- projection_outbox: 6
- artifact: 5
- artifact_occurrence: 5
- artifact_section: 213
- artifact_endorsement: 5

The five new evidence rows are immutable `public.chat_log` document sources. Existing
claims, candidates, and projection jobs did not change.

## Verification

- All four artifact tables have enabled and forced RLS.
- The production RLS test passed inside a transaction and rolled back.
- The owning account sees five artifacts and 213 sections through `brains_app`.
- A different UUID sees zero artifacts and zero sections through `brains_app`.
- All five artifacts have `extraction_policy=review_only`.
- All 213 sections have `retrieval_eligible=false` and `promotion_eligible=false`.
- Endorsements are four implicit references and one explicit reference; none is ratified.
- The guarded ingestion transaction committed `5/5/213/5` rows.
- Replaying the same manifest inserted zero rows in every table.
- No artifact-named Qdrant collection exists.
- `memory_claim_v1` remained green with six points.
- Brains remained active; no service restart was required.

## Current boundary

These documents are preserved and inspectable as technical/philosophical archive
material. They are not answer-time memory and cannot create claims until a later,
separately reviewed assessment and promotion workflow is implemented.
