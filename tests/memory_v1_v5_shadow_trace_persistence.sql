\set ON_ERROR_STOP on

DO $schema_checks$
DECLARE
  role_state record;
  function_state record;
BEGIN
  SELECT rolcanlogin, rolinherit, rolbypassrls, rolsuper,
         rolcreatedb, rolcreaterole
    INTO role_state
    FROM pg_roles
   WHERE rolname='memory_v5_trace_writer';
  IF NOT FOUND
     OR role_state.rolcanlogin
     OR role_state.rolinherit
     OR role_state.rolbypassrls
     OR role_state.rolsuper
     OR role_state.rolcreatedb
     OR role_state.rolcreaterole THEN
    RAISE EXCEPTION 'memory_v5_trace_writer is not restricted';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM information_schema.columns
     WHERE table_schema='memory'
       AND table_name='v5_shadow_trace_event'
       AND column_name IN (
         'answer','answer_text','canonical_text','claim','claims','evidence',
         'message','prompt','query','query_preview','system_prompt','text'
       )
  ) THEN
    RAISE EXCEPTION 'trace table contains a prohibited content column';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid='memory.v5_shadow_trace_event'::regclass
      AND relrowsecurity AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'trace table does not force RLS';
  END IF;
  SELECT p.prosecdef, p.provolatile, p.proowner::regrole::text AS owner_name,
         p.proconfig
    INTO function_state
    FROM pg_proc AS p
   WHERE p.oid='memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)'::regprocedure;
  IF NOT function_state.prosecdef
     OR function_state.provolatile<>'v'
     OR function_state.owner_name<>'memory_v5_trace_writer'
     OR function_state.proconfig IS DISTINCT FROM ARRAY['search_path=""']::text[] THEN
    RAISE EXCEPTION 'trace writer function boundary is invalid';
  END IF;
  IF has_table_privilege('brains_app','memory.v5_shadow_trace_event','SELECT')
     OR has_table_privilege('brains_app','memory.v5_shadow_trace_event','INSERT')
     OR has_table_privilege('brains_app','memory.v5_shadow_trace_event','UPDATE')
     OR has_table_privilege('brains_app','memory.v5_shadow_trace_event','DELETE') THEN
    RAISE EXCEPTION 'brains_app has direct trace table privileges';
  END IF;
  IF NOT has_function_privilege(
    'brains_app',
    'memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app lacks controlled trace writer execution';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM pg_proc AS p,
           LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) AS acl
     WHERE p.oid='memory.record_v5_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer)'::regprocedure
       AND acl.grantee=0
       AND acl.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute the trace writer';
  END IF;
END
$schema_checks$;

SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_v5_shadow_trace_v1(
      'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
      repeat('3',64),repeat('4',64),'ok','evaluated','personal_recall',
      'profile',repeat('5',64),repeat('6',64),1,0,0,0,
      '{"not_visible":1}'::jsonb,24,4,500,'medium',1
    );
    RAISE EXCEPTION 'missing actor was accepted';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$missing_actor$;

BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
CREATE TEMP TABLE first_result ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('4',64),'ok','evaluated','personal_recall',
  'profile',repeat('5',64),repeat('6',64),1,0,0,0,
  '{"not_visible":1}'::jsonb,24,4,500,'medium',1
);
CREATE TEMP TABLE replay_result ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('4',64),'ok','evaluated','personal_recall',
  'profile',repeat('5',64),repeat('6',64),1,0,0,0,
  '{"not_visible":1}'::jsonb,24,4,500,'medium',1
);
DO $result_checks$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM first_result WHERE outcome='applied' AND rows_written=1
  ) OR NOT EXISTS (
    SELECT 1 FROM replay_result WHERE outcome='replayed' AND rows_written=0
  ) OR (SELECT trace_event_id FROM first_result)
       IS DISTINCT FROM (SELECT trace_event_id FROM replay_result)
     OR (SELECT trace_manifest_sha256 FROM first_result)
       IS DISTINCT FROM (SELECT trace_manifest_sha256 FROM replay_result) THEN
    RAISE EXCEPTION 'trace apply/replay contract failed';
  END IF;
END
$result_checks$;
DO $mismatch$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_v5_shadow_trace_v1(
      'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
      repeat('3',64),repeat('4',64),'ok','evaluated','personal_recall',
      'profile',repeat('5',64),repeat('6',64),2,0,0,0,
      '{"not_visible":2}'::jsonb,24,4,500,'medium',1
    );
    RAISE EXCEPTION 'mismatched replay was accepted';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END
$mismatch$;
DO $direct_access$
BEGIN
  BEGIN
    PERFORM count(*) FROM memory.v5_shadow_trace_event;
    RAISE EXCEPTION 'brains_app read the trace table directly';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$direct_access$;
COMMIT;

BEGIN;
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
CREATE TEMP TABLE second_owner_result ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('7',64),
  repeat('8',64),repeat('9',64),'skipped','turn_intent:tech',NULL,NULL,
  repeat('a',64),repeat('a',64),0,0,0,0,'{}'::jsonb,
  24,4,500,'medium',0
);
DO $second_owner_check$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM second_owner_result WHERE outcome='applied' AND rows_written=1
  ) THEN
    RAISE EXCEPTION 'second owner trace was not independently applied';
  END IF;
END
$second_owner_check$;
COMMIT;

RESET SESSION AUTHORIZATION;

DO $stored_checks$
BEGIN
  IF (SELECT count(*) FROM memory.v5_shadow_trace_event)<>2
     OR (SELECT count(DISTINCT owner_user_id)
           FROM memory.v5_shadow_trace_event)<>2
     OR EXISTS (
       SELECT 1 FROM memory.v5_shadow_trace_event
       WHERE database_writes<>0 OR qdrant_writes<>0
          OR prompt_injection OR answer_model_exposure OR retrieval_activation
     ) THEN
    RAISE EXCEPTION 'stored trace rows violate the bounded owner contract';
  END IF;
END
$stored_checks$;

SET ROLE memory_v5_trace_writer;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',false
);
DO $append_only_update$
BEGIN
  BEGIN
    UPDATE memory.v5_shadow_trace_event SET status='error';
    RAISE EXCEPTION 'trace update was accepted';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$append_only_update$;
DO $append_only_delete$
BEGIN
  BEGIN
    DELETE FROM memory.v5_shadow_trace_event;
    RAISE EXCEPTION 'trace delete was accepted';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$append_only_delete$;
RESET ROLE;

SELECT 'memory_v1_v5_shadow_trace_persistence: PASS' AS result;
