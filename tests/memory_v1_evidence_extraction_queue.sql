\set ON_ERROR_STOP on

DO $security$
DECLARE
  enqueue_oid regprocedure :=
    'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)';
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname='memory_extraction_queue_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_extraction_queue_maintainer role is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid=enqueue_oid
      AND prosecdef
      AND proowner='memory_extraction_queue_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'evidence extraction enqueue function is unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',enqueue_oid,'EXECUTE')
     OR EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
       WHERE procedure.oid=enqueue_oid
         AND acl.grantee=0
         AND acl.privilege_type='EXECUTE'
     ) THEN
    RAISE EXCEPTION 'evidence extraction enqueue ACL is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    WHERE relation.oid IN (
      'memory.evidence_extraction_job'::regclass,
      'memory.evidence_extraction_event'::regclass
    )
      AND relation.relrowsecurity
      AND relation.relforcerowsecurity
    GROUP BY relation.relrowsecurity,relation.relforcerowsecurity
    HAVING count(*)=2
  ) THEN
    RAISE EXCEPTION 'evidence extraction tables lack forced RLS';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.evidence_extraction_job','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_job','DELETE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_event','INSERT'
     )
     OR NOT has_table_privilege(
       'brains_app','memory.evidence_extraction_job','SELECT'
     )
     OR NOT has_table_privilege(
       'brains_app','memory.evidence_extraction_event','SELECT'
     ) THEN
    RAISE EXCEPTION 'brains_app extraction queue ACL is unsafe';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_direct_job_insert_denied(
  p_owner uuid,
  p_evidence uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.evidence_extraction_job(
      owner_user_id,evidence_id,intake_terminal_id,selector_version,
      evidence_content_sha256,route,intake_reason_code
    ) VALUES (
      p_owner,p_evidence,gen_random_uuid(),'20260716_queue_test',
      repeat('0',64),'relational_extraction','eligible_unprocessed'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app directly inserted an extraction job';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_enqueue_conflict(
  p_evidence uuid,
  p_hash text,
  p_route text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.enqueue_owner_evidence_extraction_v1(
      p_evidence,'20260716_queue_test',p_hash,p_route,
      'eligible_unprocessed'
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'conflicting extraction enqueue was accepted';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_cross_owner_enqueue_denied(
  p_evidence uuid,
  p_hash text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.enqueue_owner_evidence_extraction_v1(
      p_evidence,'20260716_queue_test',p_hash,
      'relational_extraction','eligible_unprocessed'
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner extraction enqueue was accepted';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_job_identity_immutable(p_job uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence_extraction_job
    SET route='artifact_assessment'
    WHERE job_id=p_job;
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'extraction job identity was mutable';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_event_append_only(p_job uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence_extraction_event
    SET details='{"changed":true}'::jsonb
    WHERE job_id=p_job;
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'extraction event was mutable';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);
SELECT evidence_id AS owner_a_evidence_id,
       content_sha256 AS owner_a_evidence_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','queue_test','owner-a-eligible',
  'Owner A eligible queue fixture.','2026-07-16T18:00:00Z',
  1,1,'owner-a-eligible','low','{"test":true}'::jsonb
)
\gset

SELECT pg_temp.assert_direct_job_insert_denied(
  'c1111111-1111-4111-8111-111111111111',
  :'owner_a_evidence_id'
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_queue_test',100,NULL
)
WHERE outcome='eligible'
  AND route='relational_extraction'
  AND reason_code='eligible_unprocessed';

SELECT job_id AS queued_job_id,
       intake_terminal_id AS queued_terminal_id,
       1 / ((apply_outcome='applied')::integer) AS enqueue_applied
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'owner_a_evidence_id','20260716_queue_test',
  :'owner_a_evidence_hash','relational_extraction',
  'eligible_unprocessed'
)
\gset

SELECT 1 / ((count(*)=0)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_queue_test',100,NULL
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE job_id=:'queued_job_id'
  AND intake_terminal_id=:'queued_terminal_id'
  AND status='pending'
  AND route='relational_extraction';
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence_intake_terminal
WHERE terminal_id=:'queued_terminal_id'
  AND outcome='dispatched'
  AND reason_code='eligible_dispatched';
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE job_id=:'queued_job_id'
  AND event_type='queued'
  AND to_status='pending';

SELECT 1 / ((job_id=:'queued_job_id')::integer),
       1 / ((intake_terminal_id=:'queued_terminal_id')::integer),
       1 / ((apply_outcome='replayed')::integer)
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'owner_a_evidence_id','20260716_queue_test',
  :'owner_a_evidence_hash','relational_extraction',
  'eligible_unprocessed'
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id='c1111111-1111-4111-8111-111111111111';
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='c1111111-1111-4111-8111-111111111111';

SELECT pg_temp.assert_enqueue_conflict(
  :'owner_a_evidence_id',repeat('f',64),'relational_extraction'
);

SELECT set_config(
  'app.user_id','d2222222-2222-4222-8222-222222222222',true
);
SELECT evidence_id AS owner_b_evidence_id,
       content_sha256 AS owner_b_evidence_hash
FROM memory.record_owner_evidence_v1(
  'user_statement','queue_test','owner-b-eligible',
  'Owner B eligible queue fixture.','2026-07-16T18:01:00Z',
  1,1,'owner-b-eligible','low','{"test":true}'::jsonb
)
\gset
SELECT 1 / ((count(*)=0)::integer)
FROM memory.evidence_extraction_job;
SELECT 1 / ((count(*)=1)::integer)
FROM memory.plan_owner_evidence_intake_v1(
  '20260716_queue_test',100,NULL
)
WHERE evidence_id=:'owner_b_evidence_id';

SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);
SELECT pg_temp.assert_cross_owner_enqueue_denied(
  :'owner_b_evidence_id',:'owner_b_evidence_hash'
);

RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);
SELECT pg_temp.assert_job_identity_immutable(:'queued_job_id');
SELECT pg_temp.assert_event_append_only(:'queued_job_id');

ROLLBACK;

SELECT 'memory_v1_evidence_extraction_queue: PASS' AS result;
