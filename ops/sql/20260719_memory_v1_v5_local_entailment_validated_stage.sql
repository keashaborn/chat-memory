BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $upgrade$
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
    RAISE EXCEPTION 'validated-stage entailment compatibility requires sage';
  END IF;
  IF to_regclass('memory.v5_local_validated_stage_admission') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM pg_constraint
       WHERE conrelid='memory.v5_local_packet_stage_admission'::regclass
         AND conname='v5_local_packet_stage_admission_policy_decision_check'
     ) THEN
    RAISE EXCEPTION 'validated-stage admission schema is absent';
  END IF;
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(new_plan IN plan_def)=0 THEN
    IF position(old_plan IN plan_def)=0 THEN
      RAISE EXCEPTION 'local entailment planner definition drifted';
    END IF;
    plan_def:=replace(plan_def,old_plan,new_plan);
    EXECUTE plan_def;
  END IF;
  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(new_register IN register_def)=0 THEN
    IF position(old_register IN register_def)=0 THEN
      RAISE EXCEPTION 'local entailment register definition drifted';
    END IF;
    register_def:=replace(register_def,old_register,new_register);
    EXECUTE register_def;
  END IF;
END
$upgrade$;

COMMIT;
