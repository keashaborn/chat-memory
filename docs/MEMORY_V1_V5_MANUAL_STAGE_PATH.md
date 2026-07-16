# Memory V1 V5 manual shadow-stage path

Status: tools implemented; no production evidence or V5 staging rows written.

Server: seebx backend. Verbal Sage, RESSE, and Resse-Train are not involved.

## Boundaries

`scripts/memory_v1_v5_source_evidence.py` has two modes:

- `preflight` re-reads selected `public.chat_log` rows under forced RLS, verifies
  owner, source type, timestamp, and SHA-256, then records a zero-write plan.
- `apply` requires a clean exact Git commit, a mode-`0600` authorization valid
  for no more than 30 minutes, the exact plan hash, an environment gate, and an
  explicit confirmation phrase. It inserts/reuses canonical `memory.evidence`
  and writes one append-only `evidence_ingest_batch` plus row audit records in
  the same transaction. Replay writes zero rows.

Evidence apply cannot invoke V5 extraction, staging, resolution, projection,
Qdrant, retrieval, or prompts.

`scripts/memory_v1_v5_stage_preflight.py` consumes one passed `store=false`
evaluation packet only after it resolves to exactly one active owner-scoped
canonical evidence row. It rechecks content and every source-span hash, queries
only owner-visible entities and aliases, and builds the trusted resolution
packet plus deterministic stage request ID. Its output is mode `0600` and
contains exact packet text/hashes for later staging.

The deterministic resolver permits automatic linking only for the trusted
owner self entity or one unique, exact, low-sensitivity, non-corrective,
role-free owner-local name/alias match. It requires review or defers:

- new named entities;
- same-name ambiguity;
- relationship roles without graph support;
- medium/high/restricted context;
- corrections;
- role-only and anonymous mentions;
- projects without a trusted project binding.

The stage preflight never calls `memory.stage_relational_packet_v5`. A later
hash-locked apply tool must remain limited to that single controlled function
and must prove exact replay writes zero rows before entity resolution is
considered.
