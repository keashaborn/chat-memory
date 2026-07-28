\set ON_ERROR_STOP on

DO $test$
BEGIN
  IF to_regprocedure(
       'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
     ) IS NULL THEN
    RAISE EXCEPTION 'zero-atom V5.2 route functions are absent';
  END IF;
  IF has_function_privilege(
       'PUBLIC',
       'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'PUBLIC',
       'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'zero-atom V5.2 route ACL is invalid';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='memory.v5_2_local_packet_route_event'::regclass
      AND conname='v5_2_local_packet_route_event_reason_code_check'
      AND pg_get_constraintdef(oid)
            LIKE '%deferral_only_review_unresolved_v5_2%'
  ) THEN
    RAISE EXCEPTION 'zero-atom unresolved reason constraint is absent';
  END IF;
END
$test$;

SELECT 'memory_v1_v5_2_zero_atom_deferral_route: PASS' AS result;
