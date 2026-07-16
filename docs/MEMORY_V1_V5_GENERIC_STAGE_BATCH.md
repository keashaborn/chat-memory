# Memory V1 V5 generic reviewed stage batch

Server: seebx backend. Status: implementation and production-clone test only.
No production staging is authorized by this phase.

`scripts/memory_v1_v5_stage_batch.py` replaces the record-specific
`memory_v1_v5_stage_apply.py` operational path. It accepts one closed manifest
containing 1–25 hash-locked `memory_v1_v5_stage_preflight_v1` bundles for
exactly one owner.

The `plan` command is read-only. It requires:

- a clean Git worktree;
- mode-0600 manifest and bundles under one non-peer-writable review root;
- exact bundle, packet, schema, source, and row-budget hashes;
- one owner across every bundle;
- active canonical evidence matching each packet source under that owner,
  verified through `memory.preflight_relational_stage_bundle_v5`; and
- function-only access for `brains_app`.

The preflight function returns only the requested evidence UUID and a verified
flag. It is owned by the restricted writer role, exposes no content, grants no
table access, and returns the same not-found error for absent and cross-owner
evidence. Production currently retains pre-existing owner-RLS-scoped
`brains_app` evidence read/write grants for legacy ingestion callers. This
migration neither uses nor changes those grants; they must be revoked only
after those callers move to controlled APIs.

The `apply` command additionally requires a mode-0600, 30-minute authorization
bound to the plan SHA, owner, Git commit, bundle count, and total new-row
budget. It takes one owner advisory lock and calls only
`memory.stage_relational_packet_v5` inside one transaction. Any bundle mismatch
rolls back the entire owner batch. A second transaction must replay every
bundle with zero writes.

An extraction packet with zero mentions and zero observations still creates
the two append-only staging/audit rows. This records a reviewed
question-only/transient/no-memory outcome and prevents repeated extraction
without inventing a claim.

This runner cannot review or apply entity resolution, create durable claims,
stage or apply projections, access Qdrant, retrieve memory, or influence a
prompt.

`tools/memory_v1_v5_stage_batch_production_clone.sh` restores a fresh production
backup into a disposable PostgreSQL 16 clone and proves:

- empty and factual packets apply atomically for one synthetic owner;
- exact replay writes zero rows;
- a cross-owner bundle is rejected before writes;
- a post-plan bundle mutation is rejected before writes;
- the empty packet is durably audited;
- the second owner remains unchanged; and
- no entity-resolution apply, projection, Qdrant, retrieval, or prompt path is
  invoked.

The next boundary is a dry-run owner/evidence intake selector that emits
reviewable V5 extraction work. Production application of any new reviewed
bundle remains separate.
