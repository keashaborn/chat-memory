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
