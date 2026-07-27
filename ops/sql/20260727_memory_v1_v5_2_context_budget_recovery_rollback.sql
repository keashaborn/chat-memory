BEGIN;

DROP FUNCTION IF EXISTS
  memory.requeue_owner_v5_2_context_budget_failure_v1(
    uuid,uuid,text,uuid,uuid,integer,text,text
  );

COMMIT;
