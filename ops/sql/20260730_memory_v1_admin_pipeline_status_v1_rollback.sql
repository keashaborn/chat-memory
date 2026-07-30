BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'pipeline-status rollback requires sage'
      USING ERRCODE = '42501';
  END IF;
END
$preflight$;

REVOKE EXECUTE ON FUNCTION memory.read_owner_pipeline_status_v1()
FROM brains_app;
DROP FUNCTION memory.read_owner_pipeline_status_v1();

DO $policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.evidence'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.observation'::regclass,
    'memory.observation_entity_binding'::regclass,
    'memory.observation_entailment_v5'::regclass,
    'memory.projection_plan_item'::regclass,
    'memory.projection_plan_observation'::regclass,
    'memory.projection_review'::regclass,
    'memory.claim'::regclass,
    'memory.claim_observation'::regclass,
    'memory.final_answer_memory_binding_v1'::regclass
  ] LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS admin_pipeline_status_read ON %s',
      target
    );
  END LOOP;
END
$policies$;

REVOKE SELECT ON
  memory.evidence,
  memory.evidence_extraction_packet_v5_local,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_entailment_v5,
  memory.projection_plan_item,
  memory.projection_plan_observation,
  memory.projection_review,
  memory.claim,
  memory.claim_observation,
  memory.final_answer_memory_binding_v1
FROM memory_pipeline_status_reader_v1;
REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
FROM memory_pipeline_status_reader_v1;
REVOKE USAGE ON SCHEMA memory
FROM memory_pipeline_status_reader_v1;
DROP ROLE memory_pipeline_status_reader_v1;

COMMIT;
