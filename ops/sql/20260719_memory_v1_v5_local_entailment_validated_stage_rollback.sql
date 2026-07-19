BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
DECLARE
  plan_def text;
  register_def text;
  old_plan constant text :=
    'stage.decision=''auto_stage_eligible''';
  new_plan constant text :=
    'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'')';
  old_register constant text :=
    'source.stage_decision<>''auto_stage_eligible''';
  new_register constant text :=
    'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'')';
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'validated-stage entailment rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_entailment_assessment AS assessment
    JOIN memory.v5_local_packet_stage_admission AS stage
      ON stage.owner_user_id=assessment.owner_user_id
     AND stage.admission_id=assessment.stage_admission_id
    WHERE stage.decision='validated_entity_stage'
  ) THEN
    RAISE EXCEPTION 'refusing rollback with validated-stage entailments';
  END IF;
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(old_plan IN plan_def)=0 THEN
    IF position(new_plan IN plan_def)=0 THEN
      RAISE EXCEPTION 'local entailment planner definition drifted';
    END IF;
    EXECUTE replace(plan_def,new_plan,old_plan);
  END IF;
  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(old_register IN register_def)=0 THEN
    IF position(new_register IN register_def)=0 THEN
      RAISE EXCEPTION 'local entailment register definition drifted';
    END IF;
    EXECUTE replace(register_def,new_register,old_register);
  END IF;
END
$rollback$;

COMMIT;
