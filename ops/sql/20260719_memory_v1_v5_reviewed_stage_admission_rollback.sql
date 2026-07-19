BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $empty$
BEGIN
  IF to_regclass('memory.v5_local_reviewed_stage_admission') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.v5_local_reviewed_stage_admission
     ) THEN
    RAISE EXCEPTION 'reviewed-stage admissions exist; rollback is unsafe';
  END IF;
END
$empty$;

DO $entailment_compatibility$
DECLARE
  plan_def text;
  register_def text;
  current_plan constant text :=
    'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'')';
  prior_plan constant text :=
    'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'')';
  current_register constant text :=
    'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'')';
  prior_register constant text :=
    'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'')';
BEGIN
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(current_plan IN plan_def)>0 THEN
    EXECUTE replace(plan_def,current_plan,prior_plan);
  END IF;
  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(current_register IN register_def)>0 THEN
    EXECUTE replace(register_def,current_register,prior_register);
  END IF;
END
$entailment_compatibility$;

DROP FUNCTION IF EXISTS
  memory.register_owner_v5_reviewed_stage_admission_v1(
    uuid,uuid,uuid,uuid,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_reviewed_stage_admission_v1(integer);
DROP FUNCTION IF EXISTS memory.v5_reviewed_stage_source_v1(uuid);
DROP TABLE IF EXISTS memory.v5_local_reviewed_stage_admission;

DROP POLICY IF EXISTS reviewed_stage_access
  ON memory.v5_local_packet_stage_admission;
DO $read_policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_local_packet_review_artifact'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.relational_operation_request'::regclass,
    'memory.entity_resolution_plan'::regclass,
    'memory.entity_resolution_review'::regclass,
    'memory.entity_resolution_apply'::regclass,
    'memory.observation_entity_binding'::regclass
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS reviewed_stage_read ON %s',target);
  END LOOP;
END
$read_policies$;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_policy_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_decision_check
  CHECK (
    (decision='auto_stage_eligible'
      AND policy_version='memory_v1_v5_local_auto_stage_policy_v1')
    OR
    (decision='validated_entity_stage'
      AND policy_version='memory_v1_v5_local_entity_validation_policy_v1')
  );

DROP OWNED BY memory_v5_reviewed_stage_maintainer;
DROP ROLE IF EXISTS memory_v5_reviewed_stage_maintainer;
COMMIT;
