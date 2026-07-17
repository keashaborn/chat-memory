\set ON_ERROR_STOP on

DO $security$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid='memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'::regprocedure
      AND prosecdef
      AND proowner='memory_extraction_worker_maintainer'::regrole
      AND proconfig @> ARRAY['search_path=pg_catalog']::text[]
  ) OR NOT has_function_privilege(
    'brains_app',
    'memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)',
    'EXECUTE'
  ) OR has_function_privilege(
    'public',
    'memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'bound exact-job function security contract failed';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_exact_claim_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
  state text;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS state=RETURNED_SQLSTATE;
    denied := state IN ('22023','23503','23505','23514','42501','P0002');
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe exact-job claim was accepted: %',p_sql;
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT
  1/((job_id='788d0258-7227-46e2-8382-d13e6a122722')::integer),
  1/((status='processing')::integer),
  1/((attempts=1)::integer),
  1/((evidence_content_sha256='ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778')::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.claim_owner_bound_evidence_job_v5(
  '9f6f663b-a844-5e86-ad11-97ef7458c3d3',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'relational_extraction','memory-v1-canary-test',300,1
);

SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.claim_owner_bound_evidence_job_v5(
  '9f6f663b-a844-5e86-ad11-97ef7458c3d3',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'relational_extraction','memory-v1-canary-test',300,1
);

SELECT pg_temp.assert_exact_claim_denied(
  $sql$SELECT * FROM memory.claim_owner_bound_evidence_job_v5(
    '11111111-2222-4333-8444-555555555555',
    '788d0258-7227-46e2-8382-d13e6a122722',
    repeat('0',64),'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','memory-v1-canary-test',300,1
  )$sql$
);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT pg_temp.assert_exact_claim_denied(
  $sql$SELECT * FROM memory.claim_owner_bound_evidence_job_v5(
    '22222222-3333-4444-8555-666666666666',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'relational_extraction','memory-v1-canary-test',300,1
  )$sql$
);

RESET SESSION AUTHORIZATION;

SELECT
  1/((SELECT count(*) FROM memory.evidence_extraction_job
       WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
         AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
         AND status='processing' AND attempts=1)=1)::integer,
  1/((SELECT count(*) FROM memory.evidence_extraction_event
       WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
         AND operation_id='9f6f663b-a844-5e86-ad11-97ef7458c3d3'
         AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
         AND event_type='claimed'
         AND details->>'exact_job_canary'='true')=1)::integer;

ROLLBACK;

SELECT
  1/((SELECT count(*) FROM memory.evidence_extraction_job
       WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
         AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
         AND status='pending' AND attempts=0)=1)::integer,
  1/((SELECT count(*) FROM memory.evidence_extraction_event
       WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
         AND operation_id='9f6f663b-a844-5e86-ad11-97ef7458c3d3')=0)::integer;

\echo memory_v1_v5_bound_exact_job_claim: PASS
