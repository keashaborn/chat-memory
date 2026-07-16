\set ON_ERROR_STOP on

DO $security$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)';
BEGIN
  IF has_table_privilege('brains_app','memory.evidence','INSERT')
     OR NOT has_table_privilege('brains_app','memory.evidence','SELECT')
     OR has_table_privilege('brains_app','memory.evidence','UPDATE')
     OR has_table_privilege('brains_app','memory.evidence','DELETE') THEN
    RAISE EXCEPTION 'brains_app evidence table privileges are unsafe';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR NOT has_table_privilege(
       'memory_evidence_maintainer','memory.evidence','INSERT'
     ) THEN
    RAISE EXCEPTION 'controlled evidence writer is unavailable';
  END IF;
END
$security$;

BEGIN;

CREATE FUNCTION pg_temp.assert_direct_insert_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.evidence(
      owner_user_id,kind,source_system,external_id,content,
      content_sha256,sensitivity,metadata
    ) VALUES (
      memory.current_actor_user_id(),'user_statement',
      'privilege_boundary_test','direct-insert-must-fail',
      'Direct INSERT must fail.',
      encode(public.digest(
        convert_to('Direct INSERT must fail.','UTF8'),'sha256'
      ),'hex'),
      'low','{}'::jsonb
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app retained direct evidence INSERT';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','99999999-9999-4999-8999-999999999991',true
);
SELECT pg_temp.assert_direct_insert_denied();
SELECT 1 / ((outcome='applied')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','privilege_boundary_test','controlled-writer',
  'Owner A controlled evidence.','2026-07-16T17:00:00Z',
  1,1,'privilege-boundary-owner-a','low','{"test":true}'::jsonb
);
SELECT 1 / ((outcome='replayed')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','privilege_boundary_test','controlled-writer',
  'Owner A controlled evidence.','2026-07-16T17:00:00Z',
  1,1,'privilege-boundary-owner-a','low',
  '{"test":"ignored-on-replay"}'::jsonb
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence
WHERE external_id='controlled-writer';

SELECT set_config(
  'app.user_id','99999999-9999-4999-8999-999999999992',true
);
SELECT 1 / ((count(*)=0)::integer)
FROM memory.evidence
WHERE external_id='controlled-writer';
SELECT 1 / ((outcome='applied')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','privilege_boundary_test','controlled-writer',
  'Owner B controlled evidence.','2026-07-16T17:01:00Z',
  1,1,'privilege-boundary-owner-b','low','{"test":true}'::jsonb
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence
WHERE external_id='controlled-writer';

RESET SESSION AUTHORIZATION;

DO $results$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.evidence
    WHERE source_system='privilege_boundary_test'
      AND external_id='controlled-writer'
  )<>2 THEN
    RAISE EXCEPTION 'controlled writer did not preserve owner separation';
  END IF;
END
$results$;

ROLLBACK;

SELECT 'memory_v1_revoke_direct_evidence_insert: PASS' AS result;
