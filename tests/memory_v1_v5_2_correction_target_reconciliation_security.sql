\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  function_oid regprocedure;
  relation_oid regclass :=
    'memory.entity_correction_target_reconciliation_v5_2'::regclass;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = relation_oid AND relrowsecurity AND relforcerowsecurity
  ) OR has_table_privilege('brains_app', relation_oid, 'SELECT')
    OR has_table_privilege('brains_app', relation_oid, 'INSERT')
    OR has_table_privilege('brains_app', relation_oid, 'UPDATE')
    OR has_table_privilege('brains_app', relation_oid, 'DELETE') THEN
    RAISE EXCEPTION 'correction-target table isolation or ACL is unsafe';
  END IF;
  IF pg_get_userbyid((
       SELECT relowner FROM pg_class WHERE oid = relation_oid
     )) <> 'memory_v5_writer' THEN
    RAISE EXCEPTION 'correction-target table owner is invalid';
  END IF;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.preflight_correction_target_reconciliation_v5_2(uuid,uuid,uuid,text)'::regprocedure,
    'memory.reconcile_correction_target_v5_2(uuid,uuid,uuid,uuid,text,text)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid = function_oid AND prosecdef
        AND proowner = 'memory_v5_writer'::regrole
        AND proconfig IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM unnest(proconfig) AS setting
          WHERE setting LIKE 'search_path=%'
        )
    ) OR NOT has_function_privilege('brains_app', function_oid, 'EXECUTE')
      OR EXISTS (
        SELECT 1
        FROM pg_proc AS procedure
        CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
        WHERE procedure.oid = function_oid
          AND acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
      ) THEN
      RAISE EXCEPTION 'correction-target function % has unsafe ownership or ACL',
        function_oid;
    END IF;
  END LOOP;
END
$security$;

CREATE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_correction_target_reconciliation_v5_2(
      '80b0821a-b9a5-41e5-8008-9db72b2ebfe6'::uuid,
      'cc52a6bc-1538-58d9-a935-28b09d0ab674'::uuid,
      '09308a2b-3019-4f59-8fc3-bb1fe1408a0d'::uuid,
      'rollback-only missing actor test'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'correction-target preflight accepted a missing actor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_other_owner_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  PERFORM set_config(
    'app.user_id', '557ea042-cb82-48f8-9429-472e96c957ef', true
  );
  BEGIN
    PERFORM *
    FROM memory.preflight_correction_target_reconciliation_v5_2(
      '80b0821a-b9a5-41e5-8008-9db72b2ebfe6'::uuid,
      'cc52a6bc-1538-58d9-a935-28b09d0ab674'::uuid,
      '09308a2b-3019-4f59-8fc3-bb1fe1408a0d'::uuid,
      'rollback-only other owner test'
    );
  EXCEPTION WHEN no_data_found THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'correction-target preflight crossed owner boundary';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_wrong_target_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  PERFORM set_config(
    'app.user_id', '1240822d-ac9a-4096-95aa-e2b24d36ef50', true
  );
  BEGIN
    PERFORM *
    FROM memory.preflight_correction_target_reconciliation_v5_2(
      '80b0821a-b9a5-41e5-8008-9db72b2ebfe6'::uuid,
      'cc52a6bc-1538-58d9-a935-28b09d0ab674'::uuid,
      '0c620047-926c-4298-8303-1c55b575bb8d'::uuid,
      'rollback-only wrong target test'
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'correction-target preflight accepted the wrong animal';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_missing_actor_denied();
SELECT pg_temp.assert_other_owner_denied();
SELECT pg_temp.assert_wrong_target_denied();
SELECT set_config(
  'app.user_id', '1240822d-ac9a-4096-95aa-e2b24d36ef50', true
);
SELECT source_resolution_id, observation_id, target_entity_id,
       source_action::text, source_decision_state::text, match_basis
FROM memory.preflight_correction_target_reconciliation_v5_2(
  '80b0821a-b9a5-41e5-8008-9db72b2ebfe6'::uuid,
  'cc52a6bc-1538-58d9-a935-28b09d0ab674'::uuid,
  '09308a2b-3019-4f59-8fc3-bb1fe1408a0d'::uuid,
  'rollback-only exact correction-target test'
);
RESET SESSION AUTHORIZATION;

ROLLBACK;

SELECT 'memory_v1_v5_2_correction_target_reconciliation_security: PASS'
  AS result;
