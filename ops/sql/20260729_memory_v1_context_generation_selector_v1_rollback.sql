BEGIN;

SELECT pg_advisory_xact_lock(
  hashtextextended('memory_v1_context_generation_selector_v1', 0)
);

DROP FUNCTION IF EXISTS
  memory.enqueue_owner_context_generation_retry_v1(
    uuid,uuid,uuid,uuid,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_context_generation_retry_v1(uuid);
DROP FUNCTION IF EXISTS
  memory.select_owner_contextual_generation_v1(uuid,integer);

COMMIT;
