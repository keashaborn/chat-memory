BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'reviewed-observation stage rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_2_reviewed_observation_stage_admission
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_stage_admission
    WHERE decision IN (
      'v5_2_atom_reviewed_stage','v5_2_reviewed_route_stage'
    )
  ) THEN
    RAISE EXCEPTION
      'reviewed-observation stage rows exist; additive data rollback required';
  END IF;
END
$guard$;

DO $entailment_restore$
DECLARE
  plan_def text;
  register_def text;
  old_join constant text :=
'JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id';
  new_join constant text :=
'JOIN LATERAL memory.v5_local_stage_admission_batch_v2(
    stage.admission_id
  ) AS stage_batch ON true
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.batch_id=stage_batch.batch_id';
  old_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage''';
  new_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''';
BEGIN
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(new_join IN plan_def)=0
     OR position(new_decisions IN plan_def)=0 THEN
    RAISE EXCEPTION 'local entailment planner rollback source drifted';
  END IF;
  EXECUTE replace(
    replace(plan_def,new_join,old_join),new_decisions,old_decisions
  );

  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(new_join IN register_def)=0
     OR position(new_decisions IN register_def)=0 THEN
    RAISE EXCEPTION 'local entailment register rollback source drifted';
  END IF;
  EXECUTE replace(
    replace(register_def,new_join,old_join),new_decisions,old_decisions
  );
END
$entailment_restore$;

DROP FUNCTION memory.v5_local_stage_admission_batch_v2(uuid);
DROP FUNCTION
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text
  );
DROP FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer);
DROP FUNCTION
  memory.v5_2_reviewed_observation_stage_source_v1(uuid,uuid);

DROP POLICY v5_2_reviewed_observation_stage_access
  ON memory.v5_local_packet_stage_admission;
DO $drop_read_policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_2_local_packet_route_event'::regclass,
    'memory.v5_2_atom_admission_apply'::regclass,
    'memory.v5_2_atom_admission_proposal'::regclass,
    'memory.v5_2_atom_admission_review'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.relational_operation_request'::regclass,
    'memory.entity_resolution_plan'::regclass,
    'memory.entity_resolution_review'::regclass,
    'memory.entity_resolution_apply'::regclass,
    'memory.observation'::regclass,
    'memory.observation_entity_binding'::regclass
  ] LOOP
    EXECUTE format(
      'DROP POLICY v5_2_reviewed_observation_stage_read ON %s',
      target
    );
  END LOOP;
END
$drop_read_policies$;
DROP POLICY local_entailment_batch_read
  ON memory.relational_operation_request;
DROP POLICY local_entailment_batch_read
  ON memory.v5_local_validated_stage_admission;
DROP POLICY local_entailment_batch_read
  ON memory.v5_local_reviewed_stage_admission;

DROP TABLE memory.v5_2_reviewed_observation_stage_admission;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT v5_local_packet_stage_admission_policy_decision_check,
  DROP CONSTRAINT v5_local_packet_stage_source_route_fkey,
  DROP CONSTRAINT v5_local_packet_stage_source_atom_apply_fkey,
  DROP CONSTRAINT v5_local_packet_stage_source_route_key,
  DROP CONSTRAINT v5_local_packet_stage_source_atom_apply_key,
  DROP COLUMN source_route_event_id,
  DROP COLUMN source_atom_apply_id,
  ALTER COLUMN artifact_id SET NOT NULL;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_decision_check
  CHECK (
    (decision='auto_stage_eligible'
      AND policy_version='memory_v1_v5_local_auto_stage_policy_v1')
    OR
    (decision='validated_entity_stage'
      AND policy_version='memory_v1_v5_local_entity_validation_policy_v1')
    OR
    (decision='reviewed_entity_stage'
      AND policy_version='memory_v1_v5_reviewed_stage_admission_policy_v1')
  );

ALTER TABLE memory.v5_2_local_packet_route_event
  DROP CONSTRAINT v5_2_local_packet_route_event_owner_route_key;

DROP OWNED BY memory_v5_2_reviewed_observation_stage_maintainer;
DROP ROLE memory_v5_2_reviewed_observation_stage_maintainer;

COMMIT;
