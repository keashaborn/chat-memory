# Memory V1 V5 read-only shadow-retrieval contract

Status: isolated implementation only. Not installed, indexed, runtime-active, or
prompt-visible.

Server boundary: seebx backend.

## Authority split

Qdrant may eventually generate owner-filtered semantic candidate IDs. It is
never memory authority. PostgreSQL must re-read every candidate under the
authenticated owner and validate current relational state before selection.

The V5 selector requires:

- a V5 projection contract marker and `v5:` canonical key;
- an authorized projection review and applied projection event;
- status `supported`, `uncertain`, or `disputed`;
- at least one active evidence record through an observation link;
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

The shadow result hard-codes `prompt_injection=false`,
`answer_model_exposure=false`, and `retrieval_activation=false`. A separate
runtime gate is required before any prompt integration.

## Next database boundary

A controlled read-only PostgreSQL function must load the candidate snapshots
through forced owner RLS. `brains_app` must not receive direct access to V5
staging, observation, resolution, or projection tables.
