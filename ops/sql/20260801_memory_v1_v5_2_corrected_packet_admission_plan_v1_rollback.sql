BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'corrected-packet planner rollback requires sage';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    uuid,uuid,uuid,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid);
DROP FUNCTION IF EXISTS
  memory.v5_2_corrected_packet_plan_sha_v1(jsonb);
DROP FUNCTION IF EXISTS
  memory.v5_2_packet_observation_range_v1(jsonb);

DROP TABLE IF EXISTS
  memory.v5_2_corrected_packet_admission_proposal_v1;

REVOKE SELECT ON
  memory.evidence,
  memory.v5_local_packet_supersession,
  memory.entity,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_temporal,
  memory.claim_observation,
  memory.relational_stage_batch
FROM memory_v5_2_atom_admission_maintainer;
REVOKE EXECUTE ON FUNCTION
  memory.normalize_entity_name_v5(text),
  memory.v5_2_observation_semantic_slot_v1(text,jsonb)
FROM memory_v5_2_atom_admission_maintainer;

DO $policies$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'evidence','evidence_extraction_packet_v5_local',
    'v5_local_packet_supersession','v5_2_local_packet_route_event',
    'entity','entity_mention','entity_resolution_plan',
    'entity_resolution_apply','entity_alias_observation',
    'observation','observation_entity_binding','observation_temporal',
    'claim_observation','relational_stage_batch'
  ] LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS v5_2_correction_plan_read ON memory.%I',
      table_name
    );
  END LOOP;
END
$policies$;

COMMIT;
