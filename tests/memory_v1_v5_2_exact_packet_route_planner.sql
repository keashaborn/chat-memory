\set ON_ERROR_STOP on

BEGIN;

DO $block$
DECLARE
  target_owner uuid:='1240822d-ac9a-4096-95aa-e2b24d36ef50';
  other_owner uuid:='557ea042-cb82-48f8-9429-472e96c957ef';
  target_packet uuid;
  function_owner text;
  function_config text[];
BEGIN
  IF to_regprocedure(
       'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'exact-packet planner is absent';
  END IF;

  SELECT role.rolname, proc.proconfig
  INTO function_owner, function_config
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid=proc.pronamespace
  JOIN pg_roles AS role ON role.oid=proc.proowner
  WHERE namespace.nspname='memory'
    AND proc.proname='plan_owner_v5_2_exact_packet_route_v1'
    AND pg_get_function_identity_arguments(proc.oid)='p_packet_id uuid';

  IF function_owner<>'memory_v5_2_local_router_maintainer'
     OR function_config IS DISTINCT FROM ARRAY['search_path=pg_catalog']::text[]
     OR NOT EXISTS (
       SELECT 1
       FROM pg_proc AS proc
       JOIN pg_namespace AS namespace ON namespace.oid=proc.pronamespace
       WHERE namespace.nspname='memory'
         AND proc.proname='plan_owner_v5_2_exact_packet_route_v1'
         AND proc.prosecdef
     ) THEN
    RAISE EXCEPTION 'exact-packet planner security metadata is invalid';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_proc AS proc
    CROSS JOIN LATERAL aclexplode(
      coalesce(proc.proacl,acldefault('f',proc.proowner))
    ) AS privilege
    WHERE proc.oid=
      'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
      AND privilege.grantee=0
      AND privilege.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute exact-packet planner';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app cannot execute exact-packet planner';
  END IF;

  PERFORM set_config('app.user_id',target_owner::text,true);
  SELECT packet_id INTO target_packet
  FROM memory.plan_owner_v5_2_local_packet_route_v1(25)
  WHERE route='manual_review_artifact_ready'
  ORDER BY packet_id
  LIMIT 1;
  IF target_packet IS NULL THEN
    RAISE EXCEPTION 'no owner-scoped review packet is available for test';
  END IF;
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_2_exact_packet_route_v1(target_packet)
    WHERE route='manual_review_artifact_ready'
  )<>1 THEN
    RAISE EXCEPTION 'exact-packet planner did not return the owner target';
  END IF;

  PERFORM set_config('app.user_id',other_owner::text,true);
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_exact_packet_route_v1(target_packet)
  ) THEN
    RAISE EXCEPTION 'cross-owner exact-packet planning succeeded';
  END IF;

  PERFORM set_config('app.user_id','',true);
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_exact_packet_route_v1(target_packet)
  ) THEN
    RAISE EXCEPTION 'ownerless exact-packet planning succeeded';
  END IF;
END
$block$;

ROLLBACK;
