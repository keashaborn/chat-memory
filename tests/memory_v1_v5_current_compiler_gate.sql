\set ON_ERROR_STOP on
BEGIN;

DO $test$
DECLARE
  expected_hash constant text :=
    '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2';
  signature regprocedure;
  definition text;
BEGIN
  FOREACH signature IN ARRAY ARRAY[
    'memory.plan_owner_v5_local_entity_validation_v1(integer)'::regprocedure,
    'memory.register_owner_v5_local_entity_validation_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,jsonb,text)'::regprocedure,
    'memory.plan_owner_v5_local_auto_stage_v1(integer)'::regprocedure,
    'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)'::regprocedure
  ] LOOP
    SELECT pg_get_functiondef(signature) INTO definition;
    IF strpos(definition, expected_hash) = 0 THEN
      RAISE EXCEPTION 'current compiler hash is absent from %', signature;
    END IF;
  END LOOP;
END
$test$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / ((NOT EXISTS (
  SELECT 1
  FROM memory.plan_owner_v5_local_entity_validation_v1(20) AS plan
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.packet_id = plan.packet_id
  WHERE packet.policy_compiler_sha256 <>
    '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2'
))::integer);
SELECT 1 / ((NOT EXISTS (
  SELECT 1
  FROM memory.plan_owner_v5_local_auto_stage_v1(20) AS plan
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.packet_id = plan.packet_id
  WHERE packet.policy_compiler_sha256 <>
    '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2'
))::integer);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / ((NOT EXISTS (
  SELECT 1
  FROM memory.plan_owner_v5_local_entity_validation_v1(20) AS plan
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.packet_id = plan.packet_id
  WHERE packet.owner_user_id <>
    '557ea042-cb82-48f8-9429-472e96c957ef'::uuid
))::integer);
SELECT 1 / ((NOT EXISTS (
  SELECT 1
  FROM memory.plan_owner_v5_local_auto_stage_v1(20) AS plan
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.packet_id = plan.packet_id
  WHERE packet.owner_user_id <>
    '557ea042-cb82-48f8-9429-472e96c957ef'::uuid
))::integer);
RESET SESSION AUTHORIZATION;

ROLLBACK;
SELECT 'memory_v1_v5_current_compiler_gate: PASS' AS result;
