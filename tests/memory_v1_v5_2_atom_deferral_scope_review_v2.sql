\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

SELECT set_config('test.target_owner', :'target_owner', false);
SELECT set_config('test.other_owner', :'other_owner', false);
SELECT set_config('test.profession_packet', :'profession_packet', false);
SELECT set_config('test.caregiving_packet', :'caregiving_packet', false);

DO $catalog$
DECLARE
  function_name text;
BEGIN
  FOREACH function_name IN ARRAY ARRAY[
    'memory.plan_owner_v5_2_atom_admission_v2(uuid)',
    'memory.record_owner_v5_2_atom_proposal_v2(uuid,uuid,uuid,text,text)',
    'memory.preflight_owner_v5_2_atom_review_v2(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)',
    'memory.review_owner_v5_2_atom_proposal_v2(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)',
    'memory.preflight_owner_v5_2_atom_apply_v2(uuid)',
    'memory.apply_owner_v5_2_atom_review_v2(uuid,uuid,uuid,text)'
  ] LOOP
    IF to_regprocedure(function_name) IS NULL THEN
      RAISE EXCEPTION 'V2 atom-admission function % is absent', function_name;
    END IF;
    IF NOT has_function_privilege('brains_app', function_name, 'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app lacks controlled V2 function %', function_name;
    END IF;
    IF pg_get_userbyid((
      SELECT proowner FROM pg_proc
      WHERE oid = to_regprocedure(function_name)
    )) <> 'memory_v5_2_atom_admission_maintainer' THEN
      RAISE EXCEPTION 'V2 function % has the wrong owner', function_name;
    END IF;
  END LOOP;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'memory.v5_2_atom_admission_proposal'::regclass
      AND conname = 'v5_2_atom_admission_proposal_policy_version_check'
      AND pg_get_constraintdef(oid)
        LIKE '%memory_v1_v5_2_atom_admission_policy_v2%'
  ) THEN
    RAISE EXCEPTION 'V2 policy is absent from the proposal constraint';
  END IF;
END
$catalog$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', current_setting('test.target_owner'), false);

DO $plans$
DECLARE
  profession jsonb;
  caregiving jsonb;
BEGIN
  profession := memory.plan_owner_v5_2_atom_admission_v2(
    current_setting('test.profession_packet')::uuid
  );
  IF profession#>>'{counts,source_atom_count}' <> '6'
     OR profession#>>'{counts,admitted_entity_mention_count}' <> '3'
     OR profession#>>'{counts,admitted_observation_count}' <> '2'
     OR profession#>>'{counts,admitted_comparison_hint_count}' <> '0'
     OR profession#>>'{counts,deferred_atom_count}' <> '1'
     OR profession#>>'{counts,retained_source_only_count}' <> '0'
     OR profession#>>'{counts,rejected_atom_count}' <> '0'
     OR jsonb_array_length(
          profession#>'{stage_projection,entity_mentions}'
        ) <> 3
     OR jsonb_array_length(
          profession#>'{stage_projection,observations}'
        ) <> 2
     OR jsonb_array_length(
          profession#>'{stage_projection,deferrals}'
        ) <> 0
     OR (
       SELECT array_agg(value->>'predicate' ORDER BY value->>'observation_ref')
       FROM jsonb_array_elements(
         profession#>'{stage_projection,observations}'
       ) AS item(value)
     ) <> ARRAY['occupation.works_as', 'occupation.works_as']
     OR (
       SELECT count(*)
       FROM jsonb_array_elements(profession->'atom_decisions') AS item(value)
       WHERE value->>'atom_kind' = 'observation'
         AND value->'reason_codes' ? 'source_deferral_scope_disjoint'
     ) <> 2 THEN
    RAISE EXCEPTION 'former-profession V2 plan is incorrect: %',
      profession->'counts';
  END IF;

  caregiving := memory.plan_owner_v5_2_atom_admission_v2(
    current_setting('test.caregiving_packet')::uuid
  );
  IF caregiving#>>'{counts,source_atom_count}' <> '6'
     OR caregiving#>>'{counts,admitted_entity_mention_count}' <> '2'
     OR caregiving#>>'{counts,admitted_observation_count}' <> '2'
     OR caregiving#>>'{counts,admitted_comparison_hint_count}' <> '0'
     OR caregiving#>>'{counts,deferred_atom_count}' <> '2'
     OR caregiving#>>'{counts,retained_source_only_count}' <> '0'
     OR caregiving#>>'{counts,rejected_atom_count}' <> '0'
     OR jsonb_array_length(
          caregiving#>'{stage_projection,entity_mentions}'
        ) <> 2
     OR jsonb_array_length(
          caregiving#>'{stage_projection,observations}'
        ) <> 2
     OR jsonb_array_length(
          caregiving#>'{stage_projection,deferrals}'
        ) <> 0
     OR (
       SELECT array_agg(value->>'predicate' ORDER BY value->>'predicate')
       FROM jsonb_array_elements(
         caregiving#>'{stage_projection,observations}'
       ) AS item(value)
     ) <> ARRAY['relationship.caregiver_for', 'relationship.spouse_of']
     OR (
       SELECT count(*)
       FROM jsonb_array_elements(caregiving->'atom_decisions') AS item(value)
       WHERE value->>'atom_kind' = 'observation'
         AND value->'reason_codes' ? 'sensitive_manual_review_required'
     ) <> 2
     OR (
       SELECT count(*)
       FROM jsonb_array_elements(caregiving->'atom_decisions') AS item(value)
       WHERE value->>'atom_kind' = 'observation'
         AND value->'reason_codes' ? 'source_deferral_scope_disjoint'
     ) <> 2 THEN
    RAISE EXCEPTION 'caregiving V2 plan is incorrect: %',
      caregiving->'counts';
  END IF;
END
$plans$;

SELECT set_config('app.user_id', current_setting('test.other_owner'), false);

DO $isolation$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v2(
      current_setting('test.profession_packet')::uuid
    );
    RAISE EXCEPTION 'cross-owner profession plan was visible';
  EXCEPTION WHEN no_data_found THEN
    NULL;
  END;
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v2(
      current_setting('test.caregiving_packet')::uuid
    );
    RAISE EXCEPTION 'cross-owner caregiving plan was visible';
  EXCEPTION WHEN no_data_found THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
