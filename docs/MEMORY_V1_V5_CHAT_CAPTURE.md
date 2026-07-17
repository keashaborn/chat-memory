# Memory V1 V5 chat capture

## Decision

Technical and project conversations are retained, but they are not personal
profile claims. Capture, semantic classification, promotion, and retrieval are
separate stages:

1. `public.chat_log` remains the immutable authenticated source.
2. `memory_v1_v5_chat_capture.py` copies each user turn to owner-scoped
   `memory.evidence`. It excludes assistant turns and makes no model call.
3. Selector versions beginning with `20260717_v2` queue uncategorized evidence
   for V5 extraction. Evidence-ingest audit rows and frozen V4 consolidation
   jobs do not count as semantic processing.
4. V5 extraction routes architecture, decisions, requirements, constraints,
   roadmaps, and project status to `project_knowledge`; personal claims and
   response preferences remain separate lanes.
5. Governance creates revisions and supersession links. It does not overwrite
   source evidence.
6. Project knowledge is retrieved only for an owner and matching project or
   technical intent. Technical-turn suppression continues to block unrelated
   biography/profile injection; it does not suppress capture.

## Security and write boundary

- Every read and write sets `app.user_id` and is constrained by forced RLS.
- The worker authenticates as `brains_app` and writes evidence only through
  `memory.record_owner_evidence_v1`.
- Source/evidence hash conflicts fail the entire transaction.
- Dry run and exact replay are zero-write.
- Reports contain source IDs, SHA-256 hashes, timestamps, counts, and outcomes;
  they contain no chat text.
- The capture worker cannot write candidates, claims, projects, Qdrant, Redis,
  or prompt state and does not import an external model client.

## Production state at design time

The V4 chat consolidation trigger and timer are intentionally disabled. The
admin owner has five manually governed project heads and five archived design
artifacts. A read-only audit found 279 admin user turns and five Kelly user
turns without canonical evidence; the two synthetic test owners had complete
evidence coverage. This backlog is preserved by capture first and processed by
the V5 queue only after installation.

## Clone proof

`tools/memory_v1_v5_chat_capture_production_clone.sh` restores a production
snapshot into a disposable database, installs the compatibility function, and
proves:

- only user turns are captured;
- capture changes only `memory.evidence`;
- a V4 consolidation job cannot suppress V5 intake;
- an ingest-audit row cannot suppress V5 intake;
- dispatch writes only the append-only intake/extraction queue records;
- replay is zero-write;
- the second owner cannot see the first owner's evidence;
- reports contain no source prose;
- no external model or Qdrant writer is reachable.

This phase does not enable the live extraction worker, durable project
promotion, project retrieval, or prompt influence.
