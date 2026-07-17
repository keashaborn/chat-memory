# Memory V1 V5 offline replay materialization

Status: deterministic, zero-write bridge. It does not activate retrieval or prompt influence.

`scripts/memory_v1_relational_v5_specialized_replay.py` replays a hash-locked saved model packet against the current deterministic normalization and evaluation code. The replay now emits the complete normalized extraction packet plus its source identity, evaluation, owner, pipeline version, and before/after zero-write proof.

`scripts/memory_v1_relational_v5_specialized_materialize.py` accepts only a SHA-256-bound replay that is `store=false`, made zero external-model calls, passed evaluation and integrity checks, and proved PostgreSQL and Qdrant unchanged. It emits one full report in `zero_write_relational_v5_specialized_materialized_evaluation` mode. The report binds the replay SHA-256, materializer commit, and normalization policy version.

The canonical-evidence and V5 staging preflight readers accept this mode only when those provenance bindings are intact. They continue to re-read the owner-scoped source/evidence and verify source hashes and spans. A materialized report cannot write evidence, staging, projection, Qdrant, retrieval, or prompts by itself.

This path exists for deterministic server-side corrections such as expanding an explicit plural relation (`they all live in ...`) into separate atomic sibling residence observations. It must not be used to flip an evaluation result, rewrite source meaning, or bypass a failed integrity check.
