BEGIN;

DO $disable_local_entailment$
DECLARE
  plan_definition text;
  register_definition text;
  old_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''';
  new_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'',''legacy_applied_stage_entailment''';
  old_plan_guard constant text :=
'    AND stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'')
    AND evidence.status=''active''';
  new_plan_guard constant text :=
'    AND stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'',''legacy_applied_stage_entailment'')
    AND memory.v5_local_stage_observation_allowed_v1(
      stage.admission_id,o.observation_id,o.observation_sha256
    )
    AND evidence.status=''active''';
  old_register_guard constant text :=
'  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id;';
  new_register_guard constant text :=
'  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id
    AND memory.v5_local_stage_observation_allowed_v1(
      stage.admission_id,o.observation_id,o.observation_sha256
    );';
BEGIN
  plan_definition := pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(new_plan_guard IN plan_definition) > 0 THEN
    plan_definition := replace(
      plan_definition,
      new_plan_guard,
      old_plan_guard
    );
  END IF;
  IF position(new_decisions IN plan_definition) > 0 THEN
    plan_definition := replace(
      plan_definition,
      new_decisions,
      old_decisions
    );
  END IF;
  EXECUTE plan_definition;

  register_definition := pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(
      uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
      memory.observation_entailment_decision_v5,text,jsonb,text
    )'::regprocedure
  );
  IF position(new_register_guard IN register_definition) > 0 THEN
    register_definition := replace(
      register_definition,
      new_register_guard,
      old_register_guard
    );
  END IF;
  IF position(new_decisions IN register_definition) > 0 THEN
    register_definition := replace(
      register_definition,
      new_decisions,
      old_decisions
    );
  END IF;
  EXECUTE register_definition;
END
$disable_local_entailment$;

REVOKE ALL ON FUNCTION
  memory.register_owner_v5_legacy_stage_compat_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text,text,integer,text,text,text
  )
FROM brains_app, PUBLIC;

ALTER FUNCTION memory.plan_owner_v5_local_entailment_v1(integer)
  OWNER TO memory_v5_local_entailment_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
) OWNER TO memory_v5_local_entailment_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_entailment_v1(integer)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.register_owner_v5_local_entailment_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
    memory.observation_entailment_decision_v5,text,jsonb,text
  )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_entailment_v1(integer),
  memory.register_owner_v5_local_entailment_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
    memory.observation_entailment_decision_v5,text,jsonb,text
  )
TO brains_app;

COMMIT;
