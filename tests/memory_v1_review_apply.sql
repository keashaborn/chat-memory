\set ON_ERROR_STOP on

DO $$
DECLARE
  unsafe text;
  function_name text;
  function_owner name;
  function_security_definer boolean;
  function_config text[];
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
  INTO unsafe
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname = 'memory'
    AND c.relname IN (
      'preference_revision',
      'preference_revision_evidence',
      'preference_apply_event',
      'project_space_registration_event',
      'project_knowledge_apply_event'
    )
    AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity);
  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'review/apply tables missing enabled+forced RLS: %', unsafe;
  END IF;

  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO unsafe
  FROM (VALUES
    ('preference_revision'),
    ('preference_revision_evidence'),
    ('preference_apply_event'),
    ('project_space_registration_event'),
    ('project_knowledge_apply_event')
  ) AS protected(table_name)
  WHERE NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'SELECT'
    )
    OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'INSERT'
    )
    OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'UPDATE'
    )
    OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'DELETE'
    );
  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'review/apply table grants are unsafe: %', unsafe;
  END IF;

  FOREACH function_name IN ARRAY ARRAY[
    'register_project_space',
    'review_preference_candidate',
    'review_project_candidate',
    'apply_preference_candidate',
    'apply_project_candidate'
  ]
  LOOP
    SELECT owner.rolname, proc.prosecdef, proc.proconfig
    INTO function_owner, function_security_definer, function_config
    FROM pg_proc AS proc
    JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    JOIN pg_roles AS owner ON owner.oid = proc.proowner
    WHERE namespace.nspname = 'memory'
      AND proc.proname = function_name;
    IF function_owner <> 'memory_review_maintainer'
       OR NOT function_security_definer
       OR function_config IS DISTINCT FROM
          ARRAY['search_path=pg_catalog', 'row_security=on']::text[] THEN
      RAISE EXCEPTION 'unsafe function ownership/configuration: %', function_name;
    END IF;
  END LOOP;

  IF has_function_privilege(
       'brains_app', 'memory.review_request_sha(jsonb)', 'EXECUTE'
     )
     OR has_function_privilege(
       'brains_app', 'memory.guard_preference_revision_insert()', 'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can execute internal review/apply helpers';
  END IF;
END
$$;

BEGIN;
SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, status
) VALUES
  (
    'a1000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'memory_v1_review_apply_test', 'pref-1',
    'Use concise, direct responses.', repeat('1', 64), 'active'
  ),
  (
    'a1000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'memory_v1_review_apply_test', 'pref-2',
    'Use concise answers unless details are requested.', repeat('2', 64), 'active'
  ),
  (
    'a1000000-0000-4000-8000-000000000003',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'memory_v1_review_apply_test', 'project-1',
    'Memory ownership is the Supabase user UUID.', repeat('3', 64), 'active'
  ),
  (
    'a1000000-0000-4000-8000-000000000004',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'memory_v1_review_apply_test', 'project-2',
    'Vantage identifiers are not memory owners or filters.', repeat('4', 64), 'active'
  ),
  (
    'a1000000-0000-4000-8000-000000000005',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'memory_v1_review_apply_test', 'redacted-before-apply',
    'This evidence will be redacted before apply.', repeat('5', 64), 'active'
  );

INSERT INTO memory.preference_candidate(
  candidate_id, owner_user_id, preference_class, preference_domain,
  preference_key, value, polarity, scope, explicit, stability,
  surface_policy, extraction_confidence, sensitivity, candidate_hash,
  extractor, extractor_version, metadata
) VALUES
  (
    'a2000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'response', 'response_style', 'response_style.concise',
    '{"mode":"concise"}'::jsonb, 'prefer', '{}'::jsonb, true, 'stable',
    'silent_style_influence', 0.950, 'low', repeat('a', 64),
    'review-apply-test', '1.0.0', '{"version":1}'::jsonb
  ),
  (
    'a2000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'response', 'response_style', 'response_style.concise',
    '{"mode":"concise_unless_asked"}'::jsonb, 'prefer', '{}'::jsonb,
    true, 'stable', 'silent_style_influence', 0.970, 'low', repeat('b', 64),
    'review-apply-test', '1.0.0', '{"version":2}'::jsonb
  );

INSERT INTO memory.preference_candidate_evidence(
  owner_user_id, candidate_id, evidence_id
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a2000000-0000-4000-8000-000000000001',
    'a1000000-0000-4000-8000-000000000001'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a2000000-0000-4000-8000-000000000002',
    'a1000000-0000-4000-8000-000000000002'
  );

SELECT review_id
FROM memory.review_preference_candidate(
  'a2000000-0000-4000-8000-000000000001',
  'a3000000-0000-4000-8000-000000000001',
  repeat('a', 64), 'accept', 'user', NULL,
  'Explicit user response preference.', ARRAY['explicit_user_statement']
);

-- Exact request replay returns the same row and does not add a review.
SELECT review_id
FROM memory.review_preference_candidate(
  'a2000000-0000-4000-8000-000000000001',
  'a3000000-0000-4000-8000-000000000001',
  repeat('a', 64), 'accept', 'user', NULL,
  'Explicit user response preference.', ARRAY['explicit_user_statement']
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM memory.review_preference_candidate(
      'a2000000-0000-4000-8000-000000000001',
      'a3000000-0000-4000-8000-000000000001',
      repeat('a', 64), 'defer', 'user', NULL,
      'Changed request.', ARRAY['changed_request']
    );
  EXCEPTION WHEN invalid_parameter_value THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'changed preference review replay was accepted';
  END IF;

  blocked := false;
  BEGIN
    PERFORM memory.review_preference_candidate(
      'a2000000-0000-4000-8000-000000000002',
      'a3000000-0000-4000-8000-000000000002',
      repeat('b', 64), 'accept', 'job', 'test-job',
      'Jobs cannot accept.', ARRAY['job_review']
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'job accepted a preference candidate';
  END IF;
END
$$;

SELECT event_id
FROM memory.apply_preference_candidate(
  (
    SELECT review_id FROM memory.preference_candidate_review
    WHERE request_id = 'a3000000-0000-4000-8000-000000000001'
  ),
  'a4000000-0000-4000-8000-000000000001', NULL,
  '{"test":"first-preference-apply"}'::jsonb
);

SELECT event_id
FROM memory.apply_preference_candidate(
  (
    SELECT review_id FROM memory.preference_candidate_review
    WHERE request_id = 'a3000000-0000-4000-8000-000000000001'
  ),
  'a4000000-0000-4000-8000-000000000001', NULL,
  '{"test":"first-preference-apply"}'::jsonb
);

SELECT review_id
FROM memory.review_preference_candidate(
  'a2000000-0000-4000-8000-000000000002',
  'a3000000-0000-4000-8000-000000000003',
  repeat('b', 64), 'accept', 'user', NULL,
  'User refined the response preference.', ARRAY['explicit_refinement']
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM memory.apply_preference_candidate(
      (
        SELECT review_id FROM memory.preference_candidate_review
        WHERE request_id = 'a3000000-0000-4000-8000-000000000003'
      ),
      'a4000000-0000-4000-8000-000000000002', NULL,
      '{"test":"stale-preference-apply"}'::jsonb
    );
  EXCEPTION WHEN serialization_failure THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'stale preference apply was accepted';
  END IF;
END
$$;

SELECT event_id
FROM memory.apply_preference_candidate(
  (
    SELECT review_id FROM memory.preference_candidate_review
    WHERE request_id = 'a3000000-0000-4000-8000-000000000003'
  ),
  'a4000000-0000-4000-8000-000000000003',
  (
    SELECT current_revision_id FROM memory.user_preference
    WHERE preference_key = 'response_style.concise'
  ),
  '{"test":"second-preference-apply"}'::jsonb
);

SELECT project_id
FROM memory.register_project_space(
  'a5000000-0000-4000-8000-000000000001',
  'verbal-sage', 'Verbal Sage', '{"test":true}'::jsonb
);

SELECT project_id
FROM memory.register_project_space(
  'a5000000-0000-4000-8000-000000000001',
  'verbal-sage', 'Verbal Sage', '{"test":true}'::jsonb
);

INSERT INTO memory.project_knowledge_candidate(
  candidate_id, owner_user_id, project_id, knowledge_kind, knowledge_key,
  canonical_text, document_state, authority_level, authority_source,
  extraction_confidence, sensitivity, candidate_hash, extractor,
  extractor_version, metadata
) VALUES
  (
    'a6000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    (SELECT project_id FROM memory.project_space WHERE project_key = 'verbal-sage'),
    'constraint', 'memory.owner_boundary',
    'Memory ownership is the Supabase user UUID.', 'ratified',
    'user_ratified', '{"review":"user"}'::jsonb, 0.990, 'medium',
    repeat('c', 64), 'review-apply-test', '1.0.0', '{"version":1}'::jsonb
  ),
  (
    'a6000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    (SELECT project_id FROM memory.project_space WHERE project_key = 'verbal-sage'),
    'constraint', 'memory.owner_boundary',
    'Memory ownership and filtering use only the Supabase user UUID; vantage IDs are excluded.',
    'ratified', 'user_ratified', '{"review":"user"}'::jsonb, 0.995, 'medium',
    repeat('d', 64), 'review-apply-test', '1.0.0', '{"version":2}'::jsonb
  ),
  (
    'a6000000-0000-4000-8000-000000000003',
    '11111111-1111-4111-8111-111111111111',
    (SELECT project_id FROM memory.project_space WHERE project_key = 'verbal-sage'),
    'constraint', 'memory.unverified_status',
    'Unverified status.', 'unverified', 'unverified', '{}'::jsonb,
    0.500, 'medium', repeat('e', 64), 'review-apply-test', '1.0.0', '{}'::jsonb
  );

INSERT INTO memory.project_knowledge_candidate_evidence(
  owner_user_id, project_id, candidate_id, evidence_id
)
SELECT '11111111-1111-4111-8111-111111111111'::uuid, project_id,
       'a6000000-0000-4000-8000-000000000001'::uuid,
       'a1000000-0000-4000-8000-000000000003'::uuid
FROM memory.project_space WHERE project_key = 'verbal-sage'
UNION ALL
SELECT '11111111-1111-4111-8111-111111111111'::uuid, project_id,
       'a6000000-0000-4000-8000-000000000002'::uuid,
       'a1000000-0000-4000-8000-000000000004'::uuid
FROM memory.project_space WHERE project_key = 'verbal-sage'
UNION ALL
SELECT '11111111-1111-4111-8111-111111111111'::uuid, project_id,
       'a6000000-0000-4000-8000-000000000003'::uuid,
       'a1000000-0000-4000-8000-000000000005'::uuid
FROM memory.project_space WHERE project_key = 'verbal-sage';

SELECT review_id
FROM memory.review_project_candidate(
  'a6000000-0000-4000-8000-000000000001',
  'a7000000-0000-4000-8000-000000000001', repeat('c', 64),
  'accept', 'user', NULL, 'Ratified owner boundary.', ARRAY['user_ratified']
);

SELECT event_id
FROM memory.apply_project_candidate(
  (
    SELECT review_id FROM memory.project_knowledge_candidate_review
    WHERE request_id = 'a7000000-0000-4000-8000-000000000001'
  ),
  'a8000000-0000-4000-8000-000000000001', NULL,
  '{"test":"first-project-apply"}'::jsonb
);

SELECT review_id
FROM memory.review_project_candidate(
  'a6000000-0000-4000-8000-000000000002',
  'a7000000-0000-4000-8000-000000000002', repeat('d', 64),
  'accept', 'user', NULL, 'Ratified owner boundary refinement.',
  ARRAY['user_ratified', 'superseding_detail']
);

SELECT event_id
FROM memory.apply_project_candidate(
  (
    SELECT review_id FROM memory.project_knowledge_candidate_review
    WHERE request_id = 'a7000000-0000-4000-8000-000000000002'
  ),
  'a8000000-0000-4000-8000-000000000002',
  (
    SELECT revision_id
    FROM memory.project_knowledge_revision
    WHERE knowledge_id = (
      SELECT knowledge_id FROM memory.project_knowledge_head
      WHERE knowledge_key = 'memory.owner_boundary'
    )
    ORDER BY revision_number DESC LIMIT 1
  ),
  '{"test":"second-project-apply"}'::jsonb
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM memory.review_project_candidate(
      'a6000000-0000-4000-8000-000000000003',
      'a7000000-0000-4000-8000-000000000003', repeat('e', 64),
      'accept', 'user', NULL, 'Must not accept unverified knowledge.',
      ARRAY['unverified']
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'unverified project candidate was accepted';
  END IF;

  blocked := false;
  PERFORM set_config('app.user_id', '22222222-2222-4222-8222-222222222222', true);
  BEGIN
    PERFORM memory.apply_project_candidate(
      (
        SELECT review_id FROM memory.project_knowledge_candidate_review
        WHERE request_id = 'a7000000-0000-4000-8000-000000000002'
      ),
      'a8000000-0000-4000-8000-000000000003', NULL, '{}'::jsonb
    );
  EXCEPTION WHEN check_violation OR no_data_found OR invalid_parameter_value THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B applied actor A project review';
  END IF;
  PERFORM set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);
END
$$;

DO $$
BEGIN
  IF (SELECT count(*) FROM memory.preference_candidate_review
      WHERE candidate_id = 'a2000000-0000-4000-8000-000000000001') <> 1
     OR (SELECT count(*) FROM memory.preference_apply_event
         WHERE request_id = 'a4000000-0000-4000-8000-000000000001') <> 1
     OR (SELECT revision_number FROM memory.user_preference
         WHERE preference_key = 'response_style.concise') <> 2
     OR (SELECT count(*) FROM memory.preference_revision) <> 2
     OR (SELECT count(*) FROM memory.preference_revision_evidence) <> 2
     OR (SELECT count(*) FROM memory.project_space_registration_event) <> 1
     OR (SELECT count(*) FROM memory.project_knowledge_revision) <> 2
     OR (SELECT count(*) FROM memory.project_knowledge_apply_event) <> 2 THEN
    RAISE EXCEPTION 'review/apply deterministic row counts are incorrect';
  END IF;
END
$$;

RESET ROLE;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.preference_revision
    SET metadata = '{"tampered":true}'::jsonb
    WHERE revision_number = 1;
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'append-only preference revision was updated';
  END IF;
END
$$;

ROLLBACK;
