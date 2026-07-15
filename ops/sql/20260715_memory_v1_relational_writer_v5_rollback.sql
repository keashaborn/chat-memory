BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 relational writer V5 rollback must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.relational_operation_request') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.relational_operation_request) THEN
    RAISE EXCEPTION 'refusing writer rollback: operation requests exist';
  END IF;
  IF to_regclass('memory.relational_stage_batch') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.relational_stage_batch) THEN
    RAISE EXCEPTION 'refusing writer rollback: stage batches exist';
  END IF;
END
$block$;

DROP FUNCTION IF EXISTS memory.apply_entity_resolution_v5(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS memory.preflight_entity_resolution_apply_v5(uuid, uuid);
DROP FUNCTION IF EXISTS memory.review_entity_resolution_v5(
  uuid, uuid, memory.entity_review_decision, text, text
);
DROP FUNCTION IF EXISTS memory.preflight_entity_resolution_review_v5(
  uuid, memory.entity_review_decision, text
);
DROP FUNCTION IF EXISTS memory.stage_relational_packet_v5(
  uuid, uuid, text, text, text, text, text, text
);
DROP FUNCTION IF EXISTS memory.require_v5_writer_context();
DROP FUNCTION IF EXISTS memory.normalize_entity_name_v5(text);
DROP FUNCTION IF EXISTS memory.v5_digest_text(text);
DROP FUNCTION IF EXISTS memory.v5_jsonb_exact_keys(jsonb, text[]);

DROP TABLE IF EXISTS memory.relational_operation_request;
DROP TABLE IF EXISTS memory.relational_stage_batch;

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity_mention',
    'entity_resolution_plan',
    'entity_resolution_candidate',
    'entity_resolution_review',
    'entity_resolution_apply',
    'entity_alias_observation',
    'observation',
    'observation_temporal',
    'observation_entity_binding',
    'claim_observation',
    'candidate_observation'
  ]
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'USING (owner_user_id = memory.current_actor_user_id()) '
      'WITH CHECK (owner_user_id = memory.current_actor_user_id())',
      table_name
    );
  END LOOP;
END
$rls$;

DO $block$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_v5_writer') THEN
    DROP OWNED BY memory_v5_writer;
    DROP ROLE memory_v5_writer;
  END IF;
END
$block$;

COMMIT;
