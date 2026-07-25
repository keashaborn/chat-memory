BEGIN;

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_item
    WHERE target_reason_codes
      @> '["additional_supporting_observation"]'::jsonb
  ) THEN
    RAISE EXCEPTION
      'cannot roll back V5.2 reinforcement functions while rows exist';
  END IF;
END
$guard$;

DO $remove_policy_bridge$
DECLARE
  guard_definition text;
  replacement text :=
    E'      -- canonical_name_claim_source_v5_2_compat\n'
    || E'      AND NOT (\n'
    || E'        memory.v5_2_canonical_name_reinforcement_policy_bridge(\n'
    || E'          item.owner_user_id,item.plan_id,item.projection_ref,\n'
    || E'          link.observation_id\n'
    || E'        )\n'
    || E'        OR\n';
  marker text :=
    E'      -- canonical_name_claim_source_v5_2_compat\n'
    || E'      AND NOT (\n';
BEGIN
  SELECT pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  ) INTO guard_definition;
  IF strpos(
       guard_definition,
       'memory.v5_2_canonical_name_reinforcement_policy_bridge('
     )>0 THEN
    IF length(guard_definition)-length(replace(
         guard_definition,replacement,''
       ))<>length(replacement) THEN
      RAISE EXCEPTION
        'V5.2 reinforcement policy-bridge rollback source drifted';
    END IF;
    EXECUTE replace(guard_definition,replacement,marker);
  END IF;
END
$remove_policy_bridge$;

DROP FUNCTION IF EXISTS
  memory.stage_projection_reinforcement_v5_2(uuid,text,text);
DROP FUNCTION IF EXISTS
  memory.preflight_projection_reinforcement_v5_2(uuid,text);
DROP FUNCTION IF EXISTS
  memory.v5_2_canonical_name_reinforcement_policy_bridge(uuid,uuid,text,uuid);

COMMIT;
