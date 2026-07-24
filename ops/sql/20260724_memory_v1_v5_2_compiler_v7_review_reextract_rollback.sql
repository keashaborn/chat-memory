BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text
);

REVOKE SELECT ON
  memory.evidence_ingest_batch_row,
  memory.candidate,
  memory.claim_evidence
FROM memory_v5_local_reextract_maintainer;

COMMIT;
