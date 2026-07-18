\set ON_ERROR_STOP on

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'memory component thread binding apply requires sage';
  END IF;
  IF (SELECT count(*) FROM memory.project_thread_component_binding_event_v5)<>0
     OR EXISTS (
       SELECT 1 FROM memory.current_project_thread_component_binding_v5
       WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
         AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
     ) THEN
    RAISE EXCEPTION 'component binding target is not pristine';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memory.current_project_thread_binding_v5
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
      AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
      AND binding_event_id='e8738381-c3be-4395-bfb2-75bfd42949e9'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.project_component_v5
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
      AND component_id='1d3436df-cc4d-4b70-a6b9-730d91055b24'
      AND component_key='memory-v1'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.evidence
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND evidence_id='d91edb55-9355-426b-b191-573a55fe7ab2'
      AND content_sha256='ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778'
      AND status='active'
      AND source_system='public.chat_log'
      AND metadata->>'thread_id'='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
  ) THEN
    RAISE EXCEPTION 'trusted component binding provenance changed';
  END IF;
END
$preflight$;

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT 1 / ((apply_outcome='applied')::integer)
FROM memory.apply_owner_project_thread_component_binding_v5(
  'd77f1e79-e39e-428b-b9e9-4179d3e34105',
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
  'd77f1e79-e39e-428b-b9e9-4179d3e34105',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  '1d3436df-cc4d-4b70-a6b9-730d91055b24',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'd91edb55-9355-426b-b191-573a55fe7ab2',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'bind','explicit_registered_component_name'
);
RESET SESSION AUTHORIZATION;
COMMIT;

-- A new transaction must replay without another row.
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / ((apply_outcome='replayed')::integer)
FROM memory.apply_owner_project_thread_component_binding_v5(
  'd77f1e79-e39e-428b-b9e9-4179d3e34105',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  '1d3436df-cc4d-4b70-a6b9-730d91055b24',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'd91edb55-9355-426b-b191-573a55fe7ab2',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'bind','explicit_registered_component_name'
);
RESET SESSION AUTHORIZATION;
COMMIT;

-- A different actor cannot reuse the target owner's IDs.
BEGIN READ ONLY;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.apply_owner_project_thread_component_binding_v5(
      'fa42e5e2-c0f4-402e-94ef-76df7ed8499e',
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
      '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
      '1d3436df-cc4d-4b70-a6b9-730d91055b24',
      'e8738381-c3be-4395-bfb2-75bfd42949e9',
      'd91edb55-9355-426b-b191-573a55fe7ab2',
      'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
      'bind','explicit_registered_component_name'
    );
    RAISE EXCEPTION 'cross-owner component binding unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '25006' OR SQLSTATE '23514' THEN
    NULL;
  END;
  BEGIN
    PERFORM * FROM memory.read_v5_shadow_project_knowledge(
      'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
    );
    RAISE EXCEPTION 'cross-owner project shadow read unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$cross_owner$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 1 / ((count(*)=1)::integer)
FROM memory.project_thread_component_binding_event_v5
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND operation_id='d77f1e79-e39e-428b-b9e9-4179d3e34105'
  AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
  AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
  AND component_id='1d3436df-cc4d-4b70-a6b9-730d91055b24'
  AND component_key='memory-v1'
  AND source_project_binding_event_id='e8738381-c3be-4395-bfb2-75bfd42949e9'
  AND source_evidence_id='d91edb55-9355-426b-b191-573a55fe7ab2'
  AND action='bind';

BEGIN TRANSACTION READ ONLY;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.read_v5_shadow_project_knowledge(
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',4
)
WHERE project_key='verbal-sage'
  AND component_key='memory-v1'
  AND knowledge_key='architecture.memory_service'
  AND status='active'
  AND projection_review_decision='authorized'
  AND projection_apply_outcome='applied'
  AND evidence_ids=ARRAY['d91edb55-9355-426b-b191-573a55fe7ab2']
  AND observation_ids=ARRAY['258d8d96-2cbd-4296-b878-769c90533fae'];
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_memory_component_thread_binding_apply: PASS' AS result;
