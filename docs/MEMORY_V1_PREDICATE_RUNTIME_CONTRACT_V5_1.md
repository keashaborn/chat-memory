# Memory V1 V5.1 runtime contract

V5.1 expands what the private extractor may propose; it does not expand what the model may decide.

`seebx` owns queue selection, owner identity, evidence access, registry selection, hashes, normalization, validation, staging, review state, quotas, circuit breakers, and audit records. `VS-Memory-GPU` receives one bounded text record over the private tunnel and returns an untrusted JSON proposal from the pinned Qwen3-14B runtime. The GPU has no database credentials and cannot select an owner, registry, predicate policy, or write destination.

The runtime accepts exactly two historical profiles during cutover:

- V5: `memory_v1_relational_extraction_v5` + `memory_predicate_registry_v5`.
- V5.1: `memory_v1_relational_extraction_v5_1` + `memory_predicate_registry_v5_1`.

Each profile binds an artifact path and SHA-256 for its registry and normalized-packet schema. The scheduler selects the profile. The canary revalidates the binding before reading evidence or calling the private model. The normalized packet contract is derived from the validated registry, not from model output.

V5 remains readable and replayable. New V5.1 writes use new V5.1 functions and exact V5.1 foreign-key contracts. No function widens V5 acceptance in place. Mixed-version batches fail before writes.

The first runtime state is `shadow_review_staging`: append-only queue/ledger/packet, entity-resolution review, observation staging, and audit records are allowed. Claims, pattern heads, Qdrant projections, retrieval, and prompt influence remain disabled.

