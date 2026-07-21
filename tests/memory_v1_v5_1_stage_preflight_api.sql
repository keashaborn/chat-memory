\set ON_ERROR_STOP on

BEGIN;

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,observed_at,recorded_at,sensitivity,status
) VALUES
  (
    '3eeeeeee-3333-4333-8333-333333333333',
    '33333333-3333-4333-8333-333333333333',
    'user_statement','public.chat_log',
    '33333333-0001-4000-8000-000000000001',
    'Owner three evidence.',repeat('3',64),
    '2026-07-16T12:00:00Z','2026-07-16T13:00:00Z','low','active'
  ),
  (
    '4eeeeeee-4444-4444-8444-444444444444',
    '44444444-4444-4444-8444-444444444444',
    'user_statement','public.chat_log',
    '44444444-0001-4000-8000-000000000001',
    'Owner four evidence.',repeat('4',64),
    '2026-07-16T12:01:00Z','2026-07-16T13:01:00Z','low','active'
  );

CREATE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_relational_stage_bundle_v5_1(
      '3eeeeeee-3333-4333-8333-333333333333',
      '33333333-0001-4000-8000-000000000001',
      repeat('3',64),'2026-07-16T13:00:00Z'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'stage preflight accepted a missing actor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_cross_owner_hidden()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  hidden boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_relational_stage_bundle_v5_1(
      '4eeeeeee-4444-4444-8444-444444444444',
      '44444444-0001-4000-8000-000000000001',
      repeat('4',64),'2026-07-16T13:01:00Z'
    );
  EXCEPTION WHEN no_data_found THEN
    hidden := true;
  END;
  IF NOT hidden THEN
    RAISE EXCEPTION 'stage preflight disclosed cross-owner evidence';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_observed_at_not_source_recorded_at()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  rejected boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_relational_stage_bundle_v5_1(
      '3eeeeeee-3333-4333-8333-333333333333',
      '33333333-0001-4000-8000-000000000001',
      repeat('3',64),'2026-07-16T12:00:00Z'
    );
  EXCEPTION WHEN no_data_found THEN
    rejected := true;
  END;
  IF NOT rejected THEN
    RAISE EXCEPTION 'stage preflight accepted observed_at as source recorded_at';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_missing_actor_denied();
SELECT set_config(
  'app.user_id','33333333-3333-4333-8333-333333333333',true
);

SELECT 1 / ((
  SELECT verified
  FROM memory.preflight_relational_stage_bundle_v5_1(
    '3eeeeeee-3333-4333-8333-333333333333',
    '33333333-0001-4000-8000-000000000001',
    repeat('3',64),'2026-07-16T13:00:00Z'
  )
)::integer);
SELECT pg_temp.assert_observed_at_not_source_recorded_at();
SELECT pg_temp.assert_cross_owner_hidden();

RESET SESSION AUTHORIZATION;

DO $security$
DECLARE
  registry_constraint text;
BEGIN
  IF NOT has_function_privilege(
    'brains_app',
    'memory.preflight_relational_stage_bundle_v5_1(uuid,text,text,timestamptz)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app cannot execute stage preflight';
  END IF;
  IF pg_get_userbyid((
    SELECT proowner FROM pg_proc
    WHERE oid=
      'memory.preflight_relational_stage_bundle_v5_1(uuid,text,text,timestamptz)'
      ::regprocedure
  ))<>'memory_v5_writer' THEN
    RAISE EXCEPTION 'stage preflight has the wrong owner';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.preflight_relational_stage_bundle_v5_1(uuid,text,text,timestamptz)'
      ::regprocedure
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute stage preflight';
  END IF;

  IF to_regprocedure(
    'memory.stage_relational_packet_v5_1(uuid,uuid,text,text,text,text,text,text)'
  ) IS NULL
     OR NOT has_function_privilege(
       'brains_app',
       'memory.stage_relational_packet_v5_1(uuid,uuid,text,text,text,text,text,text)',
       'EXECUTE'
     )
     OR pg_get_userbyid((
       SELECT proowner FROM pg_proc
       WHERE oid=
         'memory.stage_relational_packet_v5_1(uuid,uuid,text,text,text,text,text,text)'
         ::regprocedure
     ))<>'memory_v5_writer' THEN
    RAISE EXCEPTION 'V5.1 controlled stage API ownership or ACL is invalid';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=
      'memory.stage_relational_packet_v5_1(uuid,uuid,text,text,text,text,text,text)'
      ::regprocedure
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute V5.1 controlled stage API';
  END IF;

  IF to_regprocedure(
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'
  ) IS NULL
     OR to_regprocedure(
       'memory.preflight_relational_stage_bundle_v5(uuid,text,text,timestamptz)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5 APIs were altered or removed';
  END IF;

  SELECT pg_get_constraintdef(oid)
  INTO registry_constraint
  FROM pg_constraint
  WHERE conrelid='memory.entity_resolution_plan'::regclass
    AND conname='entity_resolution_plan_predicate_registry_version_check';

  IF registry_constraint IS NULL
     OR position('memory_predicate_registry_v5' IN registry_constraint)=0
     OR position('memory_predicate_registry_v5_1' IN registry_constraint)=0 THEN
    RAISE EXCEPTION 'entity-resolution registry constraint is not exact V5/V5.1';
  END IF;
END
$security$;

ROLLBACK;

SELECT 'memory_v1_v5_1_stage_preflight_api: PASS' AS result;
