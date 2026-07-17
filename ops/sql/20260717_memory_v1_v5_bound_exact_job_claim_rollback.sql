BEGIN;

DROP FUNCTION IF EXISTS memory.claim_owner_bound_evidence_job_v5(
  uuid,uuid,text,uuid,text,text,integer,integer
);

COMMIT;
