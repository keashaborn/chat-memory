# Memory V1 preference/project durable apply dry run — 2026-07-14 UTC

## Scope

Server: seebx backend. Branch: `memory_v1_foundation`. Starting commit: `1626823e3d75b860e2edbd94779099398f034571`.

This phase designed and verified the durable apply path for three accepted preference candidates and five accepted `verbal-sage` project-knowledge candidates. Production execution was read-only. No durable records, retrieval projections, Qdrant points, or prompt changes were created.

## Authorization boundary

The production plan manifest is `memory_v1_preference_project_durable_apply_plan_20260714_v1` with:

- `dry_run_authorized=true`
- `apply_authorized=false`
- `prompt_injection=false`
- `retrieval_activation=false`

Manifest SHA-256: `2a46ebe299b705371b726a7514e650fb78ac3ba02dc31ff59c5af9c9322559f5`.

The runner refuses `--apply` unless a separate manifest has `apply_authorized=true` and the exact confirmation phrase is supplied. Changing the authorization flag changes the manifest hash.

## Locked source chain

| Source | SHA-256 |
|---|---|
| Controlled review manifest | `3c847d0e3321bdfb80416abf4a25c9752d3ae842ddb17bd11b7d17f0c9caa946` |
| Controlled review apply report | `7e3e7aff89cd7e31b5733ff373b8ccd4115c32f27ae10fbd4134b79827393ff7` |
| Controlled review replay report | `fa2385dc171afb11bd4d9ce45c5a2826962e7da1f0a9f543f8a79c242c49070e` |

Every durable item binds the candidate ID and hash, accepted review ID, review request ID, unique future apply request ID, durable key, expected current revision, and apply-event metadata.

## Revision locks and durable keys

All eight durable keys are new and collision-free. Every `expected_current_revision_id` is `null`.

| Lane | Durable key | Initial revision semantics |
|---|---|---|
| Preference | `personal_memory.jerry_deedee.out_of_context_surfacing` | Response preference; never surface as content |
| Preference | `music.prominent_vocals_and_metaphorical_lyrics` | Stable life preference; mention only when relevant |
| Preference | `music.concrete_surface_with_underlying_meaning` | Stable life preference; mention only when relevant |
| Project | `memory.fractal_monism_feedback_loop` | Proposed roadmap |
| Project | `fractal_monism.dataset_ai_feedback_history` | Historical status with source-date context |
| Project | `memory_system.hybrid_legacy_and_cards_2026_07_02` | Historical status with source-date context |
| Project | `website.ai_assisted_development_history` | Historical status with source-date context |
| Project | `behavior_change.ab_design_background_evaluation` | Proposed requirement |

The two music keys remain separate because they represent distinct recommendation dimensions. Historical project records remain historical; proposed records remain proposed.

## Exact future row budget

If separately authorized with the locked state unchanged, one serializable transaction will create exactly 32 rows:

| Table | Rows |
|---|---:|
| `memory.user_preference` | 3 |
| `memory.preference_revision` | 3 |
| `memory.preference_revision_evidence` | 3 |
| `memory.preference_apply_event` | 3 |
| `memory.project_knowledge_head` | 5 |
| `memory.project_knowledge_revision` | 5 |
| `memory.project_knowledge_revision_evidence` | 5 |
| `memory.project_knowledge_apply_event` | 5 |

Candidate, review, evidence, claim, projection-outbox, and Qdrant writes are fixed at zero. Durable storage does not itself activate retrieval or prompt injection.

## Safety model

- Application role has no direct insert, update, or delete privilege on durable tables.
- Durable writes are available only through two `SECURITY DEFINER` functions owned by `memory_review_maintainer` with fixed `search_path=pg_catalog` and `row_security=on`.
- Forced RLS remains enabled on all 18 specialized tables.
- Apply functions require the latest visible acceptance, the exact candidate hash, fully active evidence, and the expected current revision.
- Per-request and per-key transaction advisory locks prevent concurrent duplicate or stale application.
- Revisions and events are append-only; head updates are guarded and must match the inserted revision and accepted review.
- Unique request IDs and request hashes make replay idempotent and reject changed inputs.

## Verification

- Full isolated Memory V1 CI passed.
- The isolated end-to-end test created candidates, accepted reviews, and then exactly 32 durable rows.
- An authorization-disabled manifest was unable to apply.
- An incorrect confirmation was rejected.
- Exact replay returned the same event IDs with zero writes.
- Cross-owner durable visibility was zero.
- Production dry run passed all ten database safety checks in a serializable read-only transaction.
- Production retained 3 preference candidates, 3 preference reviews, 5 project candidates, and 5 project reviews.
- All 8 candidate evidence links remained active.
- Independent production SQL confirmed zero rows in all eight durable tables after the dry run.
- `brains.service` remained active with zero restarts.

Dry-run report SHA-256: `cf41f87a45d0d53172b2f64a58f64e853b7f9a4e067c1344b99ef63296d9b6dd`.

## Stop boundary

No production backup or database apply was required because this phase was read-only. The next phase requires separate authorization to create a fresh validated production backup, issue an authorization-enabled manifest, rerun the preflight, transactionally create the 32 durable rows, prove exact zero-write replay, and stop before retrieval activation.
