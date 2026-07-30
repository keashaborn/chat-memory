\set ON_ERROR_STOP on

BEGIN;

DO $role_contract$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'memory_pipeline_status_reader_v1'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'pipeline-status reader role contract failed';
  END IF;
  IF has_table_privilege(
    'brains_app',
    'memory.observation',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'brains_app gained direct observation access';
  END IF;
  IF NOT has_function_privilege(
    'brains_app',
    'memory.read_owner_pipeline_status_v1()',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app lacks pipeline-status execute access';
  END IF;
END
$role_contract$;

CREATE TEMP TABLE pipeline_status_test_result (
  owner_user_id uuid PRIMARY KEY,
  payload jsonb NOT NULL
) ON COMMIT DROP;
GRANT INSERT, SELECT ON pipeline_status_test_result TO brains_app;

SET SESSION AUTHORIZATION brains_app;

SELECT set_config('app.user_id', '', false);
DO $missing_actor$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM memory.read_owner_pipeline_status_v1();
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'pipeline status allowed a missing actor';
  END IF;
END
$missing_actor$;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  false
);
INSERT INTO pipeline_status_test_result(owner_user_id, payload)
VALUES (
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  memory.read_owner_pipeline_status_v1()
);

SELECT set_config(
  'app.user_id',
  '673d64a3-c4ba-4d1c-89e3-e0c579022fad',
  false
);
INSERT INTO pipeline_status_test_result(owner_user_id, payload)
VALUES (
  '673d64a3-c4ba-4d1c-89e3-e0c579022fad',
  memory.read_owner_pipeline_status_v1()
);

RESET SESSION AUTHORIZATION;

DO $payload_contract$
DECLARE
  result record;
  expected_evidence bigint;
  expected_observations bigint;
BEGIN
  FOR result IN
    SELECT owner_user_id, payload
    FROM pipeline_status_test_result
  LOOP
    SELECT count(*) INTO expected_evidence
    FROM memory.evidence
    WHERE owner_user_id = result.owner_user_id
      AND status::text = 'active';
    SELECT count(*) INTO expected_observations
    FROM memory.observation
    WHERE owner_user_id = result.owner_user_id;

    IF result.payload->>'schema' <> 'memory_pipeline_status_v1' THEN
      RAISE EXCEPTION 'pipeline payload schema mismatch';
    END IF;
    IF (result.payload#>>'{stages,evidence_rows}')::bigint
       <> expected_evidence THEN
      RAISE EXCEPTION 'pipeline evidence count crossed owner scope';
    END IF;
    IF (result.payload#>>'{stages,durable_observations}')::bigint
       <> expected_observations THEN
      RAISE EXCEPTION 'pipeline observation count crossed owner scope';
    END IF;
    IF result.payload::text ~* (
      'owner_user_id|evidence_id|observation_id|claim_id|'
      'content|canonical_text|packet_text'
    ) THEN
      RAISE EXCEPTION 'pipeline payload exposed identifiers or content';
    END IF;
  END LOOP;
END
$payload_contract$;

ROLLBACK;
