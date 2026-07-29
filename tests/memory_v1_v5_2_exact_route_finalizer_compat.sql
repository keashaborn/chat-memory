\set ON_ERROR_STOP on

BEGIN;

DO $test$
DECLARE
  function_count integer;
  public_execute_count integer;
  owner_mismatch_count integer;
BEGIN
  SELECT count(*) INTO function_count
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid=proc.pronamespace
  WHERE namespace.nspname='memory'
    AND proc.oid IN (
      'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)'::regprocedure,
      'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
        ::regprocedure,
      'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
        ::regprocedure,
      'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
        ::regprocedure
    );
  IF function_count<>6 THEN
    RAISE EXCEPTION 'exact route compatibility function count differs';
  END IF;

  SELECT count(*) INTO public_execute_count
  FROM pg_proc AS proc
  CROSS JOIN LATERAL aclexplode(
    coalesce(proc.proacl,acldefault('f',proc.proowner))
  ) AS privilege
  WHERE proc.oid IN (
      'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)'::regprocedure,
      'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
        ::regprocedure,
      'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
        ::regprocedure,
      'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
        ::regprocedure
    )
    AND privilege.grantee=0
    AND privilege.privilege_type='EXECUTE';
  IF public_execute_count<>0 THEN
    RAISE EXCEPTION 'exact route compatibility exposed public execute';
  END IF;

  SELECT count(*) INTO owner_mismatch_count
  FROM pg_proc AS proc
  JOIN pg_roles AS role ON role.oid=proc.proowner
  WHERE proc.oid IN (
      'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)'::regprocedure,
      'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)'::regprocedure,
      'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
        ::regprocedure,
      'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
        ::regprocedure,
      'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
        ::regprocedure
    )
    AND role.rolname<>'memory_v5_2_local_router_maintainer';
  IF owner_mismatch_count<>0 THEN
    RAISE EXCEPTION 'exact route compatibility function owner differs';
  END IF;

  IF NOT has_function_privilege(
    'brains_app',
    'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app lacks exact zero-atom planner execute';
  END IF;

  IF position(
    'plan_owner_v5_2_exact_packet_route_v1(p_packet_id)'
    IN pg_get_functiondef(
      'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
        ::regprocedure
    )
  )=0 THEN
    RAISE EXCEPTION 'terminal finalizer is not exact-packet bound';
  END IF;

  IF position(
    'plan_owner_v5_2_exact_packet_route_v1(p_packet_id)'
    IN pg_get_functiondef(
      'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
        ::regprocedure
    )
  )=0 THEN
    RAISE EXCEPTION 'review finalizer is not exact-packet bound';
  END IF;

  IF position(
    'plan_owner_v5_2_zero_atom_deferral_route_v1(p_packet_id)'
    IN pg_get_functiondef(
      'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
        ::regprocedure
    )
  )=0 THEN
    RAISE EXCEPTION 'zero-atom finalizer is not exact-packet bound';
  END IF;
END
$test$;

ROLLBACK;

SELECT 'memory_v1_v5_2_exact_route_finalizer_compat: PASS' AS result;
