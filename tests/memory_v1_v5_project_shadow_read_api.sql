\set ON_ERROR_STOP on

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_project_knowledge(
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
    );
    RAISE EXCEPTION 'missing-actor project shadow read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $direct_table_denial$
BEGIN
  BEGIN
    PERFORM 1 FROM memory.project_thread_component_binding_event_v5 LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read the component binding table';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
  BEGIN
    INSERT INTO memory.project_thread_component_binding_event_v5(
      binding_event_id,owner_user_id,operation_id,thread_id,project_id,
      component_id,component_key,action,reason_code,
      source_project_binding_event_id,source_evidence_id,
      source_evidence_content_sha256,actor_user_id,invoked_by_role
    ) VALUES (
      gen_random_uuid(),'1240822d-ac9a-4096-95aa-e2b24d36ef50',gen_random_uuid(),
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
      '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
      '1d3436df-cc4d-4b70-a6b9-730d91055b24','memory-v1','bind',
      'explicit_registered_component_name',
      'e8738381-c3be-4395-bfb2-75bfd42949e9',
      'd91edb55-9355-426b-b191-573a55fe7ab2',
      'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
      '1240822d-ac9a-4096-95aa-e2b24d36ef50','brains_app'
    );
    RAISE EXCEPTION 'brains_app directly wrote the component binding table';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT 1 / ((apply_outcome='applied')::integer)
FROM memory.apply_owner_project_thread_component_binding_v5(
  'e4868a85-976f-418a-9576-03d1c9e78300',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  '1d3436df-cc4d-4b70-a6b9-730d91055b24',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'd91edb55-9355-426b-b191-573a55fe7ab2',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'bind','explicit_registered_component_name'
);

SELECT 1 / ((apply_outcome='replayed')::integer)
FROM memory.apply_owner_project_thread_component_binding_v5(
  'e4868a85-976f-418a-9576-03d1c9e78300',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  '1d3436df-cc4d-4b70-a6b9-730d91055b24',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'd91edb55-9355-426b-b191-573a55fe7ab2',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'bind','explicit_registered_component_name'
);

SELECT 1 / ((count(*)=1)::integer)
FROM memory.read_v5_shadow_project_knowledge(
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
) AS value
WHERE value.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND value.project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
  AND value.project_key='verbal-sage'
  AND value.component_id='1d3436df-cc4d-4b70-a6b9-730d91055b24'
  AND value.component_key='memory-v1'
  AND value.knowledge_id='bdea6f5f-b1b9-484b-b1df-74ed6108973b'
  AND value.knowledge_kind='current_state'
  AND value.knowledge_key='architecture.memory_service'
  AND value.status='active'
  AND value.revision_id='4010f88d-b5ea-452e-8133-60bfd3109736'
  AND value.revision_number=1
  AND value.document_state='working'
  AND value.authority_level='user_reported'
  AND value.surface_policy='exact_project_scope_only'
  AND value.content_sha256='999d19ba170a8b74315dc77002f59686838a7ae42bc5e7f13db6eb26cd88e029'
  AND value.projection_review_decision='authorized'
  AND value.projection_apply_outcome='applied'
  AND value.evidence_ids=ARRAY['d91edb55-9355-426b-b191-573a55fe7ab2']
  AND value.observation_ids=ARRAY['258d8d96-2cbd-4296-b878-769c90533fae'];

DO $invalid_inputs$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_project_knowledge(
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',0
    );
    RAISE EXCEPTION 'zero project shadow budget unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.apply_owner_project_thread_component_binding_v5(
      'e4868a85-976f-418a-9576-03d1c9e78300',
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
      '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
      '1d3436df-cc4d-4b70-a6b9-730d91055b24',
      'e8738381-c3be-4395-bfb2-75bfd42949e9',
      'd91edb55-9355-426b-b191-573a55fe7ab2',
      'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
      'unbind','explicit_registered_component_name'
    );
    RAISE EXCEPTION 'conflicting component binding replay unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$invalid_inputs$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);

DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_project_knowledge(
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
    );
    RAISE EXCEPTION 'cross-owner project shadow read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.apply_owner_project_thread_component_binding_v5(
      '9a147207-7460-4d80-9f19-a8cfa9bbd2e0',
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
      '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
      '1d3436df-cc4d-4b70-a6b9-730d91055b24',
      'e8738381-c3be-4395-bfb2-75bfd42949e9',
      'd91edb55-9355-426b-b191-573a55fe7ab2',
      'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
      'bind','explicit_registered_component_name'
    );
    RAISE EXCEPTION 'cross-owner component binding unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

DO $maintenance_denial$
BEGIN
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_project_knowledge(
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
    );
    RAISE EXCEPTION 'maintenance project shadow read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$maintenance_denial$;

SELECT 1 / ((count(*)=0)::integer)
FROM memory.project_thread_component_binding_event_v5;

SELECT 1 / ((count(*)=3)::integer)
FROM pg_policies
WHERE schemaname='memory'
  AND policyname='owner_isolation_v5_project_reader'
  AND roles=ARRAY['memory_v5_reader']::name[];

SELECT 1 / ((
  pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.read_v5_shadow_project_knowledge(uuid,integer)'::regprocedure
  ))='memory_v5_reader'
)::integer);

SELECT 1 / ((
  pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)'::regprocedure
  ))='memory_v5_extraction_maintainer'
)::integer);

SELECT 1 / ((
  (SELECT provolatile FROM pg_proc WHERE oid=
    'memory.read_v5_shadow_project_knowledge(uuid,integer)'::regprocedure
  )='s'
)::integer);

SELECT 'memory_v1_v5_project_shadow_read_api: PASS' AS result;
