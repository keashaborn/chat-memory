\set ON_ERROR_STOP on
\pset pager off

SELECT
  1/((to_regprocedure(
    'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'
  ) IS NOT NULL)::integer),
  1/((SELECT pg_get_userbyid(proowner)
       FROM pg_proc
       WHERE oid='memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'::regprocedure
      )='memory_extraction_retry_maintainer')::integer,
  1/(has_function_privilege(
    'brains_app',
    'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)',
    'EXECUTE'
  )::integer),
  1/((NOT has_function_privilege(
    'public',
    'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)',
    'EXECUTE'
  ))::integer),
  1/((SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
       FROM pg_roles
       WHERE rolname='memory_extraction_retry_maintainer')::integer);

SELECT
  1/((SELECT count(*)
      FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='skipped' AND attempts=1
        AND evidence_content_sha256='ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778'
      )=1)::integer,
  1/((SELECT count(*)
      FROM memory.evidence_extraction_event
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND operation_id='a95e5e34-edb8-5296-be1b-ebe9e348aee0'
        AND event_type='skipped'
        AND details->>'error_class'='validator_rejected'
      )=1)::integer,
  1/((SELECT count(*)
      FROM memory.evidence_extraction_packet_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
      )=0)::integer;

CREATE OR REPLACE FUNCTION pg_temp.assert_retry_denied(statement text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  EXECUTE statement;
  RAISE EXCEPTION 'statement unexpectedly succeeded';
EXCEPTION
  WHEN insufficient_privilege OR check_violation OR invalid_parameter_value THEN
    NULL;
END
$function$;

BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT *
FROM memory.requeue_owner_skipped_evidence_job_v5(
  'b1111111-1111-4111-8111-111111111111',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'a95e5e34-edb8-5296-be1b-ebe9e348aee0',
  'validator_rejected',1,'diagnostic_observability_upgrade'
) \gset retry_

SELECT
  1/((:'retry_status'='pending')::integer),
  1/((:'retry_attempts'='1')::integer),
  1/((:'retry_apply_outcome'='applied')::integer),
  1/((SELECT count(*)
      FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='pending' AND attempts=1 AND last_error IS NULL
      )=1)::integer,
  1/((SELECT count(*)
      FROM memory.evidence_extraction_event
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='b1111111-1111-4111-8111-111111111111'
        AND event_type='queued'
        AND from_status='skipped' AND to_status='pending'
        AND actor_type='admin' AND actor_ref='reviewed_retry'
        AND details->>'reason_code'='diagnostic_observability_upgrade'
      )=1)::integer;

SELECT *
FROM memory.requeue_owner_skipped_evidence_job_v5(
  'b1111111-1111-4111-8111-111111111111',
  '788d0258-7227-46e2-8382-d13e6a122722',
  'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
  'e8738381-c3be-4395-bfb2-75bfd42949e9',
  'a95e5e34-edb8-5296-be1b-ebe9e348aee0',
  'validator_rejected',1,'diagnostic_observability_upgrade'
) \gset replay_

SELECT
  1/((:'replay_status'='pending')::integer),
  1/((:'replay_attempts'='1')::integer),
  1/((:'replay_apply_outcome'='replayed')::integer),
  1/((SELECT count(*)
      FROM memory.evidence_extraction_event
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='b1111111-1111-4111-8111-111111111111'
      )=1)::integer;

SELECT pg_temp.assert_retry_denied($sql$
  SELECT * FROM memory.requeue_owner_skipped_evidence_job_v5(
    'b2222222-2222-4222-8222-222222222222',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'a95e5e34-edb8-5296-be1b-ebe9e348aee0',
    'validator_rejected',1,'diagnostic_observability_upgrade'
  )
$sql$);

SELECT pg_temp.assert_retry_denied($sql$
  UPDATE memory.evidence_extraction_job
  SET status='pending'
  WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
    AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
$sql$);

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT pg_temp.assert_retry_denied($sql$
  SELECT * FROM memory.requeue_owner_skipped_evidence_job_v5(
    'b3333333-3333-4333-8333-333333333333',
    '788d0258-7227-46e2-8382-d13e6a122722',
    'ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778',
    'e8738381-c3be-4395-bfb2-75bfd42949e9',
    'a95e5e34-edb8-5296-be1b-ebe9e348aee0',
    'validator_rejected',1,'diagnostic_observability_upgrade'
  )
$sql$);

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT
  1/((SELECT count(*)
      FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='skipped' AND attempts=1
      )=1)::integer,
  1/((SELECT count(*)
      FROM memory.evidence_extraction_event
      WHERE operation_id IN (
        'b1111111-1111-4111-8111-111111111111',
        'b2222222-2222-4222-8222-222222222222',
        'b3333333-3333-4333-8333-333333333333'
      ))=0)::integer;

\echo memory_v1_v5_reviewed_retry: PASS
