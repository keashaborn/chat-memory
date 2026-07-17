# Memory V1 V5 read-only shadow-retrieval contract

Status: selector and read API are isolated and production-clone verified. They
are not installed, indexed, runtime-active, or prompt-visible.

Server boundary: seebx backend.

## Authority split

Qdrant may eventually generate owner-filtered semantic candidate IDs. It is
never memory authority. PostgreSQL must re-read every candidate under the
authenticated owner and validate current relational state before selection.

The V5 selector requires:

- a V5 projection contract marker and `v5:` canonical key;
- an authorized projection review and applied projection event;
- status `supported`, `uncertain`, or `disputed`;
- active evidence grouped without loss into `supports`, `opposes`, `qualifies`,
  and `context`;
- observation provenance;
- current temporal validity;
- an allowed sensitivity level;
- an intent-specific predicate-prefix permission; and
- the exact V5 `surface_policy`.

Candidate claims are never retrievable. `never` and
`zero_token_control_only` never become content. Exact-project records require
the matching trusted project key.

Ranking remains multidimensional. Semantic relevance, importance, and salience
are kept as separate components and compared lexicographically; V5 does not
persist or expose a synthetic final score.

Evidence stance also remains multidimensional. Supporting and contrary
evidence are never collapsed into one confidence number.

The shadow result hard-codes `prompt_injection=false`,
`answer_model_exposure=false`, and `retrieval_activation=false`. A separate
runtime gate is required before any prompt integration.

## Clone-verified read boundary

The isolated read boundary consists of:

- `ops/sql/20260716_memory_v1_v5_shadow_read_api.sql`;
- `ops/sql/20260716_memory_v1_v5_shadow_read_api_rollback.sql`;
- `tests/memory_v1_v5_shadow_read_api.sql`; and
- `tools/memory_v1_v5_shadow_read_api_production_clone.sh`.

It creates a separate restricted `memory_v5_reader` role and one controlled
read-only function. `brains_app` receives function execution only, never direct
access to V5 observation or projection tables.

The production-clone suite proves idempotent migration, missing-actor denial,
direct-table denial, cross-owner non-disclosure, bounded unique candidate
inputs, restricted role attributes, read-only function volatility, and full
rollback.

Production installation and router integration remain separate boundaries.

## Production reader installation

The restricted reader API was installed with a fresh backup and zero-row/Qdrant
verification. It is not called by the chat router.

`rag_engine/memory_v1_v5_shadow_loader.py` and
`scripts/memory_v1_v5_shadow_live_probe.py` provide the next read-only boundary:
load explicitly supplied candidate IDs through the controlled API and verify
the selector remains fail-closed. The probe does not discover candidates,
persist traces, call Qdrant, or influence prompts.

The production probe uses the retracted occupational pilot claim as a negative
control. The restricted reader must return zero rows, and the selector must
record exactly one `not_visible` rejection. This validates that a retired pilot
cannot re-enter retrieval through an explicitly supplied candidate ID.

## Candidate discovery boundary

`ClaimVectorIndex.search_claims` requests only owner, claim ID, status, and
schema-version payload fields. It verifies the returned owner and point/payload
claim IDs after the owner-filtered Qdrant search and fails closed on a mismatch.

`rag_engine/memory_v1_v5_shadow_candidate.py` wraps those hits in a deterministic
candidate envelope bound to the owner, collection, vector dimension, vector
SHA-256, limit, rank, claim IDs, and scores. The envelope does not contain query
text or claim prose and cannot write to Qdrant, Postgres, traces, or prompts.
Router integration and live candidate tracing remain inactive.
