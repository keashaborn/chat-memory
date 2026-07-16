\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)';
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_evidence_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_evidence_maintainer role is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid=function_oid
      AND prosecdef
      AND provolatile='v'
      AND proowner='memory_evidence_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'record evidence API ownership/config is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'brains_app cannot execute record evidence API';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid=function_oid
      AND acl.grantee=0
      AND acl.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute record evidence API';
  END IF;
  IF NOT has_table_privilege(
    'memory_evidence_maintainer','memory.evidence','INSERT'
  ) THEN
    RAISE EXCEPTION 'record evidence API owner cannot insert evidence';
  END IF;
END
$security$;

BEGIN;

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
    PERFORM * FROM memory.record_owner_evidence_v1(
      'user_statement','public.chat_log','record-api-missing-actor',
      'missing actor',NULL,1,1,NULL,'low','{}'::jsonb
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'record evidence API accepted a missing actor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_content_conflict()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.record_owner_evidence_v1(
      'user_statement','public.chat_log','record-api-owner-a',
      'changed content','2026-07-16T14:00:00Z',1,1,
      'record-api-owner-a','low','{"test":true}'::jsonb
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'record evidence API accepted changed replay content';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_missing_actor_denied();
SELECT set_config(
  'app.user_id','55555555-5555-4555-8555-555555555555',true
);

SELECT 1 / ((outcome='applied')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','record-api-owner-a',
  'Owner A controlled evidence.','2026-07-16T14:00:00Z',1,1,
  'record-api-owner-a','low','{"test":true}'::jsonb
);
SELECT 1 / ((outcome='replayed')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','record-api-owner-a',
  'Owner A controlled evidence.','2026-07-16T14:00:00Z',1,1,
  'record-api-owner-a','low','{"test":"ignored-on-replay"}'::jsonb
);
SELECT pg_temp.assert_content_conflict();

SELECT set_config(
  'app.user_id','66666666-6666-4666-8666-666666666666',true
);
SELECT 1 / ((outcome='applied')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','public.chat_log','record-api-owner-a',
  'Owner B controlled evidence.','2026-07-16T14:01:00Z',1,1,
  'record-api-owner-b','low','{"test":true}'::jsonb
);

RESET SESSION AUTHORIZATION;

DO $results$
BEGIN
  IF (SELECT count(*) FROM memory.evidence
      WHERE owner_user_id='55555555-5555-4555-8555-555555555555')<>1
     OR (SELECT count(*) FROM memory.evidence
      WHERE owner_user_id='66666666-6666-4666-8666-666666666666')<>1 THEN
    RAISE EXCEPTION 'record evidence API produced incorrect owner rows';
  END IF;
END
$results$;

ROLLBACK;

SELECT 'memory_v1_record_evidence_api: PASS' AS result;
