\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  FOREACH function_oid IN ARRAY ARRAY[
    'memory.preflight_owner_project_component_entity_v5(uuid,uuid)'::regprocedure,
    'memory.bootstrap_owner_project_component_entity_v5(uuid,uuid,uuid,text)'::regprocedure,
    'memory.resolve_owner_project_component_entity_candidate_v5(text,text,text)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=function_oid AND prosecdef
        AND proowner='memory_v5_writer'::regrole
        AND proconfig @> ARRAY['search_path=""']::text[]
    ) THEN
      RAISE EXCEPTION 'project component entity function % is unsafe',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR has_function_privilege('public',function_oid,'EXECUTE') THEN
      RAISE EXCEPTION 'project component entity function % has unsafe ACL',function_oid;
    END IF;
  END LOOP;
  IF has_table_privilege(
       'brains_app','memory.project_component_entity_binding_v5','INSERT'
     ) OR NOT EXISTS (
       SELECT 1 FROM pg_class
       WHERE oid='memory.project_component_entity_binding_v5'::regclass
         AND relrowsecurity AND relforcerowsecurity
     ) THEN
    RAISE EXCEPTION 'project component entity table boundary is unsafe';
  END IF;
END
$security$;

BEGIN;

INSERT INTO memory.project_space(
  project_id,owner_user_id,project_key,display_name,metadata
) VALUES
  ('ea000000-0000-4000-8000-000000000001',
   'ea111111-1111-4111-8111-111111111111',
   'verbal-sage','Verbal Sage','{"test":true}'),
  ('eb000000-0000-4000-8000-000000000001',
   'eb222222-2222-4222-8222-222222222222',
   'other','Other','{"test":true}');

INSERT INTO memory.project_component_v5(
  component_id,owner_user_id,project_id,component_key,display_name,metadata
) VALUES
  ('ea200000-0000-4000-8000-000000000001',
   'ea111111-1111-4111-8111-111111111111',
   'ea000000-0000-4000-8000-000000000001',
   'memory-v1','Memory V1','{"test":true}'),
  ('eb200000-0000-4000-8000-000000000001',
   'eb222222-2222-4222-8222-222222222222',
   'eb000000-0000-4000-8000-000000000001',
   'memory-v1','Memory V1','{"test":true}');

INSERT INTO memory.project_component_alias_v5(
  owner_user_id,project_id,component_id,normalized_alias,display_alias
) VALUES
  ('ea111111-1111-4111-8111-111111111111',
   'ea000000-0000-4000-8000-000000000001',
   'ea200000-0000-4000-8000-000000000001',
   'memory-v1','Memory V1'),
  ('ea111111-1111-4111-8111-111111111111',
   'ea000000-0000-4000-8000-000000000001',
   'ea200000-0000-4000-8000-000000000001',
   'verbal-sage-memory-v1-v5','Verbal Sage Memory V1/V5'),
  ('eb222222-2222-4222-8222-222222222222',
   'eb000000-0000-4000-8000-000000000001',
   'eb200000-0000-4000-8000-000000000001',
   'memory-v1','Memory V1');

CREATE FUNCTION pg_temp.assert_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean:=false;
  state text;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS state=RETURNED_SQLSTATE;
    denied:=state IN ('22023','23503','23505','23514','42501','P0002');
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe project component entity operation succeeded: %',p_sql;
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_denied(
  $sql$SELECT * FROM memory.preflight_owner_project_component_entity_v5(
    'ea000000-0000-4000-8000-000000000001',
    'ea200000-0000-4000-8000-000000000001'
  )$sql$
);
SELECT set_config('app.user_id','ea111111-1111-4111-8111-111111111111',true);

SELECT *
FROM memory.preflight_owner_project_component_entity_v5(
  'ea000000-0000-4000-8000-000000000001',
  'ea200000-0000-4000-8000-000000000001'
)
\gset preflight_

SELECT
  1/((:'preflight_state'='create')::integer),
  1/((:'preflight_component_key'='memory-v1')::integer),
  1/((cardinality(:'preflight_aliases'::text[])=2)::integer),
  1/((:'preflight_aliases'::text[] @> ARRAY[
    'memory v1','verbal sage memory v1 v5'
  ])::integer);

SELECT *
FROM memory.bootstrap_owner_project_component_entity_v5(
  'ea300000-0000-4000-8000-000000000001',
  'ea000000-0000-4000-8000-000000000001',
  'ea200000-0000-4000-8000-000000000001',
  :'preflight_authorization_manifest_sha256'
)
\gset applied_

SELECT
  1/((:'applied_outcome'='created')::integer),
  1/((:'applied_rows_written'='4')::integer);

SELECT
  1/((entity_id=:'applied_entity_id'::uuid)::integer),
  1/((entity_type='project')::integer),
  1/((exact_alias)::integer)
FROM memory.resolve_owner_project_component_entity_candidate_v5(
  'verbal-sage','memory-v1','verbal sage memory v1 v5'
);

SELECT
  1/((outcome='replayed')::integer),
  1/((rows_written=0)::integer)
FROM memory.bootstrap_owner_project_component_entity_v5(
  'ea300000-0000-4000-8000-000000000001',
  'ea000000-0000-4000-8000-000000000001',
  'ea200000-0000-4000-8000-000000000001',
  :'preflight_authorization_manifest_sha256'
);

SELECT pg_temp.assert_denied(
  $sql$SELECT * FROM memory.preflight_owner_project_component_entity_v5(
    'eb000000-0000-4000-8000-000000000001',
    'eb200000-0000-4000-8000-000000000001'
  )$sql$
);
SELECT pg_temp.assert_denied(
  $sql$INSERT INTO memory.project_component_entity_binding_v5(
    owner_user_id,request_id,manifest_sha256,prior_state_sha256,
    project_id,component_id,entity_id,alias_set_sha256,result,
    invoked_by_session
  ) VALUES (
    'ea111111-1111-4111-8111-111111111111',gen_random_uuid(),repeat('a',64),
    repeat('b',64),'ea000000-0000-4000-8000-000000000001',
    'ea200000-0000-4000-8000-000000000001',gen_random_uuid(),repeat('c',64),
    '{}','brains_app'
  )$sql$
);

RESET SESSION AUTHORIZATION;
SELECT
  1/((SELECT count(*)=1
      FROM memory.project_component_entity_binding_v5
      WHERE owner_user_id='ea111111-1111-4111-8111-111111111111')::integer),
  1/((SELECT count(*)=1 FROM memory.entity
      WHERE owner_user_id='ea111111-1111-4111-8111-111111111111'
        AND entity_type='project')::integer),
  1/((SELECT count(*)=2 FROM memory.entity_alias
      WHERE owner_user_id='ea111111-1111-4111-8111-111111111111'
        AND entity_id=:'applied_entity_id'::uuid)::integer);

ROLLBACK;
