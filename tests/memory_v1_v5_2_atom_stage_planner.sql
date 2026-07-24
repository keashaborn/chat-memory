\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT set_config('test.target_owner',:'target_owner',true);
SELECT set_config('test.other_owner',:'other_owner',true);
SELECT set_config('test.apply_id',:'apply_id',true);
SELECT set_config('test.evidence_id',:'evidence_id',true);
SELECT set_config('test.projection_sha256',:'projection_sha256',true);

DO $catalog$
DECLARE
  function_oid regprocedure :=
    'memory.plan_owner_v5_2_atom_stage_v1(uuid)'::regprocedure;
BEGIN
  IF (
    SELECT NOT procedure.prosecdef
      OR procedure.provolatile<>'s'
      OR owner_role.rolname<>'memory_v5_2_atom_admission_maintainer'
    FROM pg_proc AS procedure
    JOIN pg_roles AS owner_role ON owner_role.oid=procedure.proowner
    WHERE procedure.oid=function_oid
  ) THEN
    RAISE EXCEPTION 'atom stage planner function metadata is invalid';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(
         COALESCE(procedure.proacl,acldefault('f',procedure.proowner))
       ) AS privilege
       WHERE procedure.oid=function_oid
         AND privilege.grantee=0
         AND privilege.privilege_type='EXECUTE'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_atom_admission_apply','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_atom_admission_proposal','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.v5_2_atom_admission_review','SELECT'
     ) THEN
    RAISE EXCEPTION 'atom stage planner privileges are invalid';
  END IF;
END
$catalog$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'target_owner',true);

DO $target$
DECLARE
  plan jsonb;
BEGIN
  plan:=memory.plan_owner_v5_2_atom_stage_v1(
    current_setting('test.apply_id')::uuid
  );
  IF plan->>'contract_version'<>'memory_v1_v5_2_atom_stage_plan_v1'
     OR plan->>'owner_user_id'<>current_setting('test.target_owner')
     OR plan->>'apply_id'<>current_setting('test.apply_id')
     OR plan->>'evidence_id'<>current_setting('test.evidence_id')
     OR plan->>'stage_projection_sha256'
          <>current_setting('test.projection_sha256')
     OR plan#>>'{counts,entity_mentions}'<>'1'
     OR plan#>>'{counts,observations}'<>'4'
     OR plan#>>'{counts,comparison_hints}'<>'0'
     OR plan#>>'{counts,deferrals}'<>'0'
     OR jsonb_array_length(plan#>'{stage_projection,deferrals}')<>0 THEN
    RAISE EXCEPTION 'target atom stage plan differs from reviewed projection';
  END IF;
END
$target$;

SELECT set_config('app.user_id',:'other_owner',true);
DO $cross_owner$
BEGIN
  PERFORM memory.plan_owner_v5_2_atom_stage_v1(
    current_setting('test.apply_id')::uuid
  );
  RAISE EXCEPTION 'cross-owner atom stage plan unexpectedly resolved';
EXCEPTION
  WHEN SQLSTATE 'P0002' THEN NULL;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
