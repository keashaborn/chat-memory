\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname='memory_extraction_worker_maintainer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_extraction_worker_maintainer role is unsafe';
  END IF;

  FOREACH function_oid IN ARRAY ARRAY[
    'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'::regprocedure,
    'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'::regprocedure,
    'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'::regprocedure,
    'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'::regprocedure,
    'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'::regprocedure
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_proc
      WHERE oid=function_oid
        AND prosecdef
        AND proowner='memory_extraction_worker_maintainer'::regrole
        AND proconfig=ARRAY['search_path=pg_catalog']::text[]
    ) THEN
      RAISE EXCEPTION 'worker function % is unsafe',function_oid;
    END IF;
    IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
       OR EXISTS (
         SELECT 1
         FROM pg_proc AS procedure
         CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
         WHERE procedure.oid=function_oid
           AND acl.grantee=0
           AND acl.privilege_type='EXECUTE'
       ) THEN
      RAISE EXCEPTION 'worker function % has unsafe ACL',function_oid;
    END IF;
  END LOOP;

  IF NOT has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_job',
       'SELECT'
     )
     OR NOT has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_job',
       'UPDATE'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_job',
       'INSERT'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_job',
       'DELETE'
     )
     OR NOT has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_event',
       'SELECT'
     )
     OR NOT has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_event',
       'INSERT'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_event',
       'UPDATE'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence_extraction_event',
       'DELETE'
     )
     OR NOT has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence',
       'SELECT'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence',
       'INSERT'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence',
       'UPDATE'
     )
     OR has_table_privilege(
       'memory_extraction_worker_maintainer',
       'memory.evidence',
       'DELETE'
     ) THEN
    RAISE EXCEPTION 'worker maintainer table ACL is unsafe';
  END IF;

  IF has_table_privilege(
       'brains_app','memory.evidence_extraction_job','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.evidence_extraction_event','INSERT'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct worker mutation privileges';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_class
    WHERE oid IN (
      'memory.evidence'::regclass,
      'memory.evidence_extraction_job'::regclass,
      'memory.evidence_extraction_event'::regclass
    )
      AND relrowsecurity
      AND relforcerowsecurity
    GROUP BY relrowsecurity,relforcerowsecurity
    HAVING count(*)=3
  ) THEN
    RAISE EXCEPTION 'worker source/queue tables lack forced RLS';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname='memory'
      AND indexname='evidence_extraction_event_owner_operation_uidx'
      AND indexdef LIKE '%UNIQUE INDEX%'
      AND indexdef LIKE '%WHERE (operation_id IS NOT NULL)%'
  ) THEN
    RAISE EXCEPTION 'worker operation idempotency index is absent';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_worker_call_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION
    WHEN check_violation OR insufficient_privilege OR invalid_parameter_value
    THEN denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe worker call was accepted: %',p_sql;
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','worker_test','owner-a-review',
  'Owner A extraction review fixture.','2026-07-16T20:00:00Z',
  1,1,'owner-a-review','low','{"test":true}'::jsonb
)
\gset review_evidence_

SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'review_evidence_evidence_id',
  '20260716_worker_test',
  :'review_evidence_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
)
\gset review_queue_

SELECT
  job_id,
  lease_token,
  evidence_content_sha256,
  1/((status='processing')::integer) AS processing,
  1/((attempts=1)::integer) AS first_attempt,
  1/((evidence_content='Owner A extraction review fixture.')::integer)
    AS content_bound,
  1/((apply_outcome='applied')::integer) AS claim_applied
FROM memory.claim_owner_evidence_extraction_job_v1(
  'a1000000-0000-4000-8000-000000000001',
  'relational_extraction',
  'worker-test-a',
  300,
  3
)
\gset review_claim_

SELECT
  1/((job_id=:'review_claim_job_id')::integer),
  1/((lease_token=:'review_claim_lease_token')::integer),
  1/((apply_outcome='replayed')::integer)
FROM memory.claim_owner_evidence_extraction_job_v1(
  'a1000000-0000-4000-8000-000000000001',
  'relational_extraction',
  'worker-test-a',
  300,
  3
);
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE operation_id='a1000000-0000-4000-8000-000000000001';
SELECT 1/((count(*)=0)::integer)
FROM memory.claim_owner_evidence_extraction_job_v1(
  'a1000000-0000-4000-8000-000000000002',
  'relational_extraction',
  'worker-test-a',
  300,
  3
);

SELECT encode(
  public.digest(
    convert_to('{"candidate_count":1,"stage":"parsed"}'::jsonb::text,'UTF8'),
    'sha256'
  ),
  'hex'
) AS checkpoint_sha256
\gset

SELECT
  1/((checkpoint_sequence=1)::integer),
  1/((checkpoint_sha256=:'checkpoint_sha256')::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.checkpoint_owner_evidence_extraction_job_v1(
  'a2000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'review_claim_lease_token',
  'worker-test-a',
  :'review_claim_evidence_content_sha256',
  1,
  :'checkpoint_sha256',
  '{"candidate_count":1,"stage":"parsed"}'::jsonb,
  300
);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.checkpoint_owner_evidence_extraction_job_v1(
  'a2000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'review_claim_lease_token',
  'worker-test-a',
  :'review_claim_evidence_content_sha256',
  1,
  :'checkpoint_sha256',
  '{"candidate_count":1,"stage":"parsed"}'::jsonb,
  300
);
SELECT pg_temp.assert_worker_call_denied(format(
  $sql$
    SELECT *
    FROM memory.checkpoint_owner_evidence_extraction_job_v1(
      'a2000000-0000-4000-8000-000000000001',
      %L,%L,'worker-test-a',%L,2,%L,
      '{"candidate_count":1,"stage":"parsed"}'::jsonb,300
    )
  $sql$,
  :'review_claim_job_id',
  :'review_claim_lease_token',
  :'review_claim_evidence_content_sha256',
  :'checkpoint_sha256'
));

SELECT encode(
  public.digest(
    convert_to('{"candidate_ids":["candidate-a"]}'::jsonb::text,'UTF8'),
    'sha256'
  ),
  'hex'
) AS finish_sha256
\gset

SELECT 1/((apply_outcome='applied')::integer)
FROM memory.finish_owner_evidence_extraction_job_v1(
  'a3000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'review_claim_lease_token',
  'worker-test-a',
  :'review_claim_evidence_content_sha256',
  'review_required',
  :'finish_sha256',
  '{"candidate_ids":["candidate-a"]}'::jsonb
);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.finish_owner_evidence_extraction_job_v1(
  'a3000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'review_claim_lease_token',
  'worker-test-a',
  :'review_claim_evidence_content_sha256',
  'review_required',
  :'finish_sha256',
  '{"candidate_ids":["candidate-a"]}'::jsonb
);

SELECT encode(
  public.digest(
    convert_to('{"decision":"approve"}'::jsonb::text,'UTF8'),
    'sha256'
  ),
  'hex'
) AS review_sha256
\gset

SELECT 1/((apply_outcome='applied')::integer)
FROM memory.resolve_owner_evidence_extraction_review_v1(
  'a4000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'finish_sha256',
  'completed',
  :'review_sha256',
  '{"decision":"approve"}'::jsonb
);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.resolve_owner_evidence_extraction_review_v1(
  'a4000000-0000-4000-8000-000000000001',
  :'review_claim_job_id',
  :'finish_sha256',
  'completed',
  :'review_sha256',
  '{"decision":"approve"}'::jsonb
);
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE job_id=:'review_claim_job_id'
  AND status='completed'
  AND checkpoint_sequence=1
  AND checkpoint_sha256=:'checkpoint_sha256'
  AND result#>>'{final,sha256}'=:'finish_sha256'
  AND result#>>'{review,sha256}'=:'review_sha256';
SELECT 1/((count(*)=5)::integer)
FROM memory.evidence_extraction_event
WHERE job_id=:'review_claim_job_id';

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','worker_test','owner-a-retry',
  'Owner A extraction retry fixture.','2026-07-16T20:01:00Z',
  1,1,'owner-a-retry','low','{"test":true}'::jsonb
)
\gset retry_evidence_
SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'retry_evidence_evidence_id',
  '20260716_worker_test',
  :'retry_evidence_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
)
\gset retry_queue_
SELECT job_id,lease_token,evidence_content_sha256
FROM memory.claim_owner_evidence_extraction_job_v1(
  'b1000000-0000-4000-8000-000000000001',
  'relational_extraction','worker-test-a',300,2
)
\gset retry_first_
SELECT
  1/((status='error')::integer),
  1/((attempts=1)::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.fail_owner_evidence_extraction_job_v1(
  'b2000000-0000-4000-8000-000000000001',
  :'retry_first_job_id',
  :'retry_first_lease_token',
  'worker-test-a',
  :'retry_first_evidence_content_sha256',
  'SyntheticError',
  'first retryable failure',
  2
);
SELECT 1/((apply_outcome='replayed')::integer)
FROM memory.fail_owner_evidence_extraction_job_v1(
  'b2000000-0000-4000-8000-000000000001',
  :'retry_first_job_id',
  :'retry_first_lease_token',
  'worker-test-a',
  :'retry_first_evidence_content_sha256',
  'SyntheticError',
  'first retryable failure',
  2
);

RESET SESSION AUTHORIZATION;
UPDATE memory.evidence_extraction_job
SET available_at=clock_timestamp()-interval '1 second'
WHERE owner_user_id='c1111111-1111-4111-8111-111111111111'
  AND job_id=:'retry_first_job_id';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);

SELECT job_id,lease_token,evidence_content_sha256,attempts
FROM memory.claim_owner_evidence_extraction_job_v1(
  'b3000000-0000-4000-8000-000000000001',
  'relational_extraction','worker-test-a',300,2
)
\gset retry_second_
SELECT 1/((:'retry_second_attempts'::integer=2)::integer);
SELECT pg_temp.assert_worker_call_denied(format(
  $sql$
    SELECT *
    FROM memory.finish_owner_evidence_extraction_job_v1(
      'b3000000-0000-4000-8000-000000000099',
      %L,%L,'worker-test-a',%L,'completed',%L,
      '{"candidate_ids":["candidate-a"]}'::jsonb
    )
  $sql$,
  :'retry_second_job_id',
  :'retry_first_lease_token',
  :'retry_second_evidence_content_sha256',
  :'finish_sha256'
));
SELECT
  1/((status='skipped')::integer),
  1/((attempts=2)::integer),
  1/((apply_outcome='applied')::integer)
FROM memory.fail_owner_evidence_extraction_job_v1(
  'b4000000-0000-4000-8000-000000000001',
  :'retry_second_job_id',
  :'retry_second_lease_token',
  'worker-test-a',
  :'retry_second_evidence_content_sha256',
  'SyntheticError',
  'maximum-attempt failure',
  2
);

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','worker_test','owner-a-expired',
  'Owner A expired lease fixture.','2026-07-16T20:02:00Z',
  1,1,'owner-a-expired','low','{"test":true}'::jsonb
)
\gset expired_evidence_
SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'expired_evidence_evidence_id',
  '20260716_worker_test',
  :'expired_evidence_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
)
\gset expired_queue_
SELECT job_id,lease_token
FROM memory.claim_owner_evidence_extraction_job_v1(
  'c1000000-0000-4000-8000-000000000001',
  'relational_extraction','worker-test-a',300,1
)
\gset expired_claim_

RESET SESSION AUTHORIZATION;
UPDATE memory.evidence_extraction_job
SET lease_expires_at=clock_timestamp()-interval '1 second'
WHERE owner_user_id='c1111111-1111-4111-8111-111111111111'
  AND job_id=:'expired_claim_job_id';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','c1111111-1111-4111-8111-111111111111',true
);

SELECT 1/((count(*)=0)::integer)
FROM memory.claim_owner_evidence_extraction_job_v1(
  'c2000000-0000-4000-8000-000000000001',
  'relational_extraction','worker-test-a',300,1
);
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE job_id=:'expired_claim_job_id'
  AND status='skipped'
  AND lease_token IS NULL
  AND last_error='maximum extraction attempts exhausted';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE job_id=:'expired_claim_job_id'
  AND event_type='skipped'
  AND details->>'reason'='max_attempts_exhausted';

SELECT set_config(
  'app.user_id','d2222222-2222-4222-8222-222222222222',true
);
SELECT 1/((count(*)=0)::integer)
FROM memory.evidence_extraction_job;
SELECT 1/((count(*)=0)::integer)
FROM memory.evidence_extraction_event;
SELECT pg_temp.assert_worker_call_denied(format(
  $sql$
    SELECT *
    FROM memory.resolve_owner_evidence_extraction_review_v1(
      'd1000000-0000-4000-8000-000000000001',
      %L,%L,'completed',%L,'{"decision":"approve"}'::jsonb
    )
  $sql$,
  :'review_claim_job_id',
  :'finish_sha256',
  :'review_sha256'
));

ROLLBACK;

SELECT 'memory_v1_evidence_extraction_worker: PASS' AS result;
