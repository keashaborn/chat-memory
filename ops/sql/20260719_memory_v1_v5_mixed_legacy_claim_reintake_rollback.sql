BEGIN;

REVOKE ALL ON FUNCTION
  memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)
FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)
FROM PUBLIC,brains_app,memory_intake_maintainer,memory_extraction_queue_maintainer;

DROP FUNCTION IF EXISTS
  memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(
    uuid,text,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid);

COMMIT;
