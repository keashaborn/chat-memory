\set ON_ERROR_STOP on
BEGIN;

DO $test$
DECLARE
  trigger_function text;
  function_owner text;
BEGIN
  SELECT procedure.proname
    INTO trigger_function
  FROM pg_trigger AS trigger
  JOIN pg_proc AS procedure
    ON procedure.oid=trigger.tgfoid
  WHERE trigger.tgrelid='memory.relational_stage_batch'::regclass
    AND trigger.tgname='v5_local_disposition_stage_guard'
    AND NOT trigger.tgisinternal;
  IF trigger_function IS DISTINCT FROM
       'guard_disposed_evidence_from_stage_v2' THEN
    RAISE EXCEPTION 'V5.2 disposition compatibility trigger is not active';
  END IF;

  SELECT owner.rolname
    INTO function_owner
  FROM pg_proc AS procedure
  JOIN pg_roles AS owner
    ON owner.oid=procedure.proowner
  WHERE procedure.oid=
    'memory.guard_disposed_evidence_from_stage_v2()'::regprocedure;
  IF function_owner IS DISTINCT FROM
       'memory_v5_local_disposition_maintainer' THEN
    RAISE EXCEPTION 'V5.2 disposition compatibility owner is incorrect';
  END IF;

  IF has_function_privilege(
       'brains_app',
       'memory.guard_disposed_evidence_from_stage_v2()',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'brains_app must not execute the compatibility trigger directly';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(
      coalesce(
        procedure.proacl,
        acldefault('f',procedure.proowner)
      )
    ) AS privilege
    WHERE procedure.oid=
      'memory.guard_disposed_evidence_from_stage_v2()'::regprocedure
      AND privilege.grantee=0
      AND privilege.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION
      'PUBLIC must not execute the compatibility trigger directly';
  END IF;

  IF NOT has_function_privilege(
       'memory_v5_local_disposition_maintainer',
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'compatibility owner cannot call the atom authorization function';
  END IF;
  IF has_function_privilege(
       'brains_app',
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION
      'brains_app must not execute the atom authorization function directly';
  END IF;

  IF to_regprocedure(
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()'
     ) IS NULL THEN
    RAISE EXCEPTION 'authoritative V5.2 stage guards are absent';
  END IF;
END
$test$;

ROLLBACK;
