\set ON_ERROR_STOP on

DO $preflight$
DECLARE
  matched_records integer;
  pending_records integer;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'component thread binding requires sage';
  END IF;
  IF (SELECT count(*) FROM memory.project_thread_binding_event) <> 0 THEN
    RAISE EXCEPTION 'project thread binding event table is not empty';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM public.threads
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
  ) OR NOT EXISTS (
    SELECT 1
    FROM memory.project_space
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
      AND project_key='verbal-sage'
  ) OR NOT EXISTS (
    SELECT 1
    FROM memory.project_component_v5
    WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
      AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
      AND component_key='memory-v1'
  ) THEN
    RAISE EXCEPTION 'owner thread, project, or component is absent';
  END IF;

  SELECT
    count(*),
    count(*) FILTER (
      WHERE job.evidence_content_sha256
        ='ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778'
        AND lower(evidence.content)
          ~ '(^|[^a-z0-9])memory[^a-z0-9]+v1([^a-z0-9]|$)'
    )
  INTO pending_records,matched_records
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=job.owner_user_id
   AND evidence.evidence_id=job.evidence_id
  WHERE job.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
    AND job.route='relational_extraction'
    AND job.status='pending'
    AND evidence.metadata->>'thread_id'
      ='d776c8ef-7f3d-45b2-8820-4be87b7ca19d';
  IF pending_records<>7 OR matched_records<>1 THEN
    RAISE EXCEPTION
      'selector evidence changed: pending %, exact matches %',
      pending_records,matched_records;
  END IF;
END
$preflight$;

BEGIN;

CREATE FUNCTION pg_temp.assert_binding_call_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
  state text;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS state = RETURNED_SQLSTATE;
    denied := state IN ('22023','23503','23505','23514','42501','P0002');
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe project binding call was accepted: %',p_sql;
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT
  1 / ((thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d')::integer),
  1 / ((project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc')::integer),
  1 / ((project_key='verbal-sage')::integer),
  1 / ((action='bind')::integer),
  1 / ((apply_outcome='applied')::integer)
FROM memory.apply_owner_project_thread_binding_v5(
  'a8dc45c0-7a5c-5885-ad3b-5a696dbb33d6',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'bind','explicit_registered_component_name'
);

SELECT 1 / ((apply_outcome='replayed')::integer)
FROM memory.apply_owner_project_thread_binding_v5(
  'a8dc45c0-7a5c-5885-ad3b-5a696dbb33d6',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'bind','explicit_registered_component_name'
);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT pg_temp.assert_binding_call_denied(
  $sql$SELECT * FROM memory.apply_owner_project_thread_binding_v5(
    '11111111-2222-4333-8444-555555555555',
    'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
    '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
    'bind','explicit_registered_component_name'
  )$sql$
);

RESET SESSION AUTHORIZATION;

SELECT
  1 / ((SELECT count(*) FROM memory.project_thread_binding_event
         WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
           AND operation_id='a8dc45c0-7a5c-5885-ad3b-5a696dbb33d6'
           AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
           AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
           AND action='bind'
           AND reason_code='explicit_registered_component_name')=1)::integer,
  1 / ((SELECT count(*) FROM memory.current_project_thread_binding_v5
         WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
           AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
           AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc')=1)::integer,
  1 / ((SELECT count(*) FROM memory.project_thread_binding_event
         WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50')=0)::integer;

COMMIT;

-- Persisted replay in a separate transaction must remain zero-write.
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
SELECT 1 / ((apply_outcome='replayed')::integer)
FROM memory.apply_owner_project_thread_binding_v5(
  'a8dc45c0-7a5c-5885-ad3b-5a696dbb33d6',
  'd776c8ef-7f3d-45b2-8820-4be87b7ca19d',
  '08cd6a8a-5599-43d5-8d5c-b59401df8ccc',
  'bind','explicit_registered_component_name'
);
RESET SESSION AUTHORIZATION;
COMMIT;

\echo memory_v1_v5_component_thread_binding_apply: PASS
