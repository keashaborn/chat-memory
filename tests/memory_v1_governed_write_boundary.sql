\set ON_ERROR_STOP on

DO $acl$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'entity_alias',
    'candidate',
    'claim',
    'claim_revision',
    'claim_evidence',
    'claim_assessment',
    'claim_relation'
  ]
  LOOP
    IF NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'SELECT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'INSERT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'UPDATE'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'DELETE'
    ) THEN
      RAISE EXCEPTION 'unsafe brains_app ACL on memory.%', table_name;
    END IF;
  END LOOP;

  IF NOT has_function_privilege(
    'brains_app',
    'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)',
    'EXECUTE'
  ) OR NOT has_function_privilege(
    'brains_app',
    'memory.apply_projection_v5(uuid,uuid,text,uuid,text)',
    'EXECUTE'
  ) OR NOT has_function_privilege(
    'brains_app',
    'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)',
    'EXECUTE'
  ) OR NOT has_function_privilege(
    'brains_app',
    'memory.read_v5_shadow_claims(uuid[])',
    'EXECUTE'
  ) OR NOT has_function_privilege(
    'brains_app',
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'controlled governed APIs are incomplete';
  END IF;
END
$acl$;

BEGIN;

CREATE FUNCTION pg_temp.assert_governed_direct_dml_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  table_name text;
  denied boolean;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity',
    'entity_alias',
    'candidate',
    'claim',
    'claim_revision',
    'claim_evidence',
    'claim_assessment',
    'claim_relation'
  ]
  LOOP
    denied := false;
    BEGIN
      EXECUTE format('INSERT INTO memory.%I DEFAULT VALUES', table_name);
    EXCEPTION WHEN insufficient_privilege THEN
      denied := true;
    END;
    IF NOT denied THEN
      RAISE EXCEPTION 'direct INSERT unexpectedly reached memory.%', table_name;
    END IF;

    denied := false;
    BEGIN
      EXECUTE format(
        'UPDATE memory.%I SET owner_user_id=owner_user_id WHERE false',
        table_name
      );
    EXCEPTION WHEN insufficient_privilege THEN
      denied := true;
    END;
    IF NOT denied THEN
      RAISE EXCEPTION 'direct UPDATE unexpectedly reached memory.%', table_name;
    END IF;

    denied := false;
    BEGIN
      EXECUTE format('DELETE FROM memory.%I WHERE false', table_name);
    EXCEPTION WHEN insufficient_privilege THEN
      denied := true;
    END;
    IF NOT denied THEN
      RAISE EXCEPTION 'direct DELETE unexpectedly reached memory.%', table_name;
    END IF;
  END LOOP;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','99999999-9999-4999-8999-999999999991',true
);
SELECT pg_temp.assert_governed_direct_dml_denied();

SELECT 1 / ((outcome='applied')::integer)
FROM memory.record_owner_evidence_v1(
  'user_statement','governed_write_boundary_test','controlled-writer',
  'Synthetic controlled evidence.','2026-07-19T03:00:00Z',
  1,1,'governed-write-boundary-owner-a','low','{"test":true}'::jsonb
);
SELECT 1 / ((count(*)=1)::integer)
FROM memory.evidence
WHERE source_system='governed_write_boundary_test'
  AND external_id='controlled-writer';

SELECT set_config(
  'app.user_id','99999999-9999-4999-8999-999999999992',true
);
SELECT 1 / ((count(*)=0)::integer)
FROM memory.evidence
WHERE source_system='governed_write_boundary_test'
  AND external_id='controlled-writer';

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_governed_write_boundary: PASS' AS result;
