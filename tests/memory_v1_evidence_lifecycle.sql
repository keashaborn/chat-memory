\set ON_ERROR_STOP on

DO $$
DECLARE
  maintainer pg_roles%ROWTYPE;
  function_owner name;
  function_security_definer boolean;
  function_config text[];
BEGIN
  SELECT * INTO maintainer
  FROM pg_roles
  WHERE rolname = 'memory_evidence_maintainer';

  IF NOT FOUND
     OR maintainer.rolcanlogin
     OR maintainer.rolsuper
     OR maintainer.rolbypassrls
     OR maintainer.rolinherit THEN
    RAISE EXCEPTION 'memory_evidence_maintainer role is not locked down';
  END IF;

  SELECT owner.rolname, proc.prosecdef, proc.proconfig
  INTO function_owner, function_security_definer, function_config
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
  JOIN pg_roles AS owner ON owner.oid = proc.proowner
  WHERE namespace.nspname = 'memory'
    AND proc.proname = 'transition_evidence_lifecycle';

  IF function_owner <> 'memory_evidence_maintainer'
     OR NOT function_security_definer
     OR function_config IS DISTINCT FROM ARRAY['search_path=pg_catalog']::text[] THEN
    RAISE EXCEPTION 'lifecycle function ownership or search_path is unsafe';
  END IF;

  IF has_table_privilege('brains_app', 'memory.evidence', 'UPDATE')
     OR has_table_privilege('brains_app', 'memory.evidence', 'DELETE')
     OR NOT has_table_privilege('brains_app', 'memory.evidence', 'SELECT')
     OR NOT has_table_privilege('brains_app', 'memory.evidence', 'INSERT') THEN
    RAISE EXCEPTION 'brains_app evidence privileges are not insert-only';
  END IF;

  IF has_table_privilege(
       'brains_app', 'memory.evidence_lifecycle_event', 'INSERT'
     )
     OR has_table_privilege(
       'brains_app', 'memory.evidence_lifecycle_event', 'UPDATE'
     )
     OR has_table_privilege(
       'brains_app', 'memory.evidence_lifecycle_event', 'DELETE'
     )
     OR NOT has_table_privilege(
       'brains_app', 'memory.evidence_lifecycle_event', 'SELECT'
     ) THEN
    RAISE EXCEPTION 'brains_app lifecycle event privileges are unsafe';
  END IF;

  IF NOT has_function_privilege(
    'brains_app',
    'memory.transition_evidence_lifecycle(uuid,uuid,text,text,text,text,jsonb)',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app cannot execute the lifecycle function';
  END IF;
END
$$;

BEGIN;
SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.evidence(
  evidence_id,
  owner_user_id,
  kind,
  source_system,
  external_id,
  content,
  content_sha256,
  directness,
  status
) VALUES (
  'e1000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'user_statement',
  'public.chat_log',
  'lifecycle-test-source-1',
  'Content that will be redacted.',
  repeat('a', 64),
  1.000,
  'active'
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence
    SET content = 'Direct replacement must fail.'
    WHERE evidence_id = 'e1000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app directly updated evidence';
  END IF;

  blocked := false;
  BEGIN
    DELETE FROM memory.evidence
    WHERE evidence_id = 'e1000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app directly deleted evidence';
  END IF;
END
$$;

SELECT event_id
FROM memory.transition_evidence_lifecycle(
  'e1000000-0000-4000-8000-000000000001',
  'e2000000-0000-4000-8000-000000000001',
  'redact_content',
  'privacy_request',
  'user',
  'memory-v1-lifecycle-test',
  '{"test":"redaction"}'::jsonb
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence
    WHERE evidence_id = 'e1000000-0000-4000-8000-000000000001'
      AND status = 'redacted'
      AND content IS NULL
      AND content_sha256 = repeat('a', 64)
  ) THEN
    RAISE EXCEPTION 'redaction did not preserve the hash-only tombstone';
  END IF;

  IF (
    SELECT count(*)
    FROM memory.evidence_lifecycle_event
    WHERE request_id = 'e2000000-0000-4000-8000-000000000001'
      AND outcome = 'applied'
      AND prior_status = 'active'
      AND resulting_status = 'redacted'
      AND content_was_present
      AND source_copy_expected
  ) <> 1 THEN
    RAISE EXCEPTION 'redaction audit event is incomplete';
  END IF;
END
$$;

SELECT event_id
FROM memory.transition_evidence_lifecycle(
  'e1000000-0000-4000-8000-000000000001',
  'e2000000-0000-4000-8000-000000000001',
  'redact_content',
  'privacy_request',
  'user',
  'memory-v1-lifecycle-test',
  '{"test":"redaction"}'::jsonb
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  IF (
    SELECT count(DISTINCT event_id) FROM memory.evidence_lifecycle_event
    WHERE request_id = 'e2000000-0000-4000-8000-000000000001'
  ) <> 1 THEN
    RAISE EXCEPTION 'idempotent replay duplicated an event';
  END IF;

  BEGIN
    PERFORM 1
    FROM memory.transition_evidence_lifecycle(
      'e1000000-0000-4000-8000-000000000001',
      'e2000000-0000-4000-8000-000000000001',
      'delete_tombstone',
      'privacy_request',
      'user',
      'memory-v1-lifecycle-test',
      '{"test":"redaction"}'::jsonb
    );
  EXCEPTION WHEN invalid_parameter_value THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'request_id accepted different lifecycle inputs';
  END IF;

  blocked := false;
  BEGIN
    UPDATE memory.evidence_lifecycle_event
    SET reason_code = 'correction'
    WHERE request_id = 'e2000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app updated an audit event';
  END IF;

  blocked := false;
  BEGIN
    DELETE FROM memory.evidence_lifecycle_event
    WHERE request_id = 'e2000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app deleted an audit event';
  END IF;
END
$$;

INSERT INTO memory.evidence(
  evidence_id,
  owner_user_id,
  kind,
  source_system,
  external_id,
  content,
  content_sha256,
  status
) VALUES (
  'e1000000-0000-4000-8000-000000000002',
  '11111111-1111-4111-8111-111111111111',
  'user_statement',
  'memory_v1_lifecycle_test',
  'lifecycle-test-source-2',
  'Candidate source.',
  repeat('b', 64),
  'active'
);

INSERT INTO memory.candidate(
  candidate_id,
  owner_user_id,
  evidence_id,
  status,
  proposal,
  proposal_hash,
  extractor,
  extractor_version
) VALUES (
  'e3000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'e1000000-0000-4000-8000-000000000002',
  'review_required',
  '{}'::jsonb,
  repeat('c', 64),
  'lifecycle_test',
  'v1'
);

SELECT event_id
FROM memory.transition_evidence_lifecycle(
  'e1000000-0000-4000-8000-000000000002',
  'e2000000-0000-4000-8000-000000000002',
  'redact_content',
  'correction',
  'admin',
  'memory-v1-lifecycle-test',
  '{}'::jsonb
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.candidate
    SET status = 'approved'
    WHERE candidate_id = 'e3000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'candidate was approved after evidence redaction';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.candidate(
      owner_user_id, evidence_id, status, proposal,
      proposal_hash, extractor, extractor_version
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'e1000000-0000-4000-8000-000000000002',
      'review_required',
      '{}'::jsonb,
      repeat('d', 64),
      'lifecycle_test',
      'v1'
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'candidate was created after evidence redaction';
  END IF;
END
$$;

SELECT set_config('app.user_id', '22222222-2222-4222-8222-222222222222', true);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM 1
    FROM memory.transition_evidence_lifecycle(
      'e1000000-0000-4000-8000-000000000001',
      'e2000000-0000-4000-8000-000000000003',
      'delete_tombstone',
      'user_request',
      'user',
      NULL,
      '{}'::jsonb
    );
  EXCEPTION WHEN no_data_found THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B transitioned actor A evidence';
  END IF;
END
$$;

SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

SELECT event_id
FROM memory.transition_evidence_lifecycle(
  'e1000000-0000-4000-8000-000000000001',
  'e2000000-0000-4000-8000-000000000004',
  'delete_tombstone',
  'user_request',
  'user',
  NULL,
  '{}'::jsonb
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence
    WHERE evidence_id = 'e1000000-0000-4000-8000-000000000001'
      AND status = 'deleted'
      AND content IS NULL
  ) THEN
    RAISE EXCEPTION 'deleted tombstone has the wrong state';
  END IF;

  IF (
    SELECT count(*)
    FROM memory.evidence_lifecycle_event
    WHERE request_id = 'e2000000-0000-4000-8000-000000000004'
      AND prior_status = 'redacted'
      AND resulting_status = 'deleted'
      AND outcome = 'applied'
  ) <> 1 THEN
    RAISE EXCEPTION 'redacted-to-deleted audit transition is missing';
  END IF;
END
$$;

RESET ROLE;

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  PERFORM set_config(
    'app.user_id', '11111111-1111-4111-8111-111111111111', true
  );
  BEGIN
    DELETE FROM memory.evidence
    WHERE evidence_id = 'e1000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'table owner bypassed the physical-delete trigger';
  END IF;
END
$$;

ROLLBACK;

SELECT 'memory_v1_evidence_lifecycle: PASS' AS result;
