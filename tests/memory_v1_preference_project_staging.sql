\set ON_ERROR_STOP on

DO $$
DECLARE
  missing text;
  unsafe text;
  function_owner name;
  function_security_definer boolean;
  function_config text[];
  review_maintainer pg_roles%ROWTYPE;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
  INTO missing
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname = 'memory'
    AND c.relname IN (
      'preference_candidate',
      'preference_candidate_evidence',
      'preference_candidate_review',
      'preference_candidate_review_replacement',
      'project_space',
      'project_knowledge_candidate',
      'project_knowledge_candidate_evidence',
      'project_knowledge_candidate_review',
      'project_knowledge_candidate_review_replacement',
      'project_knowledge_head',
      'project_knowledge_revision',
      'project_knowledge_revision_evidence',
      'project_knowledge_relation'
    )
    AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity);

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'specialized tables missing enabled+forced RLS: %', missing;
  END IF;

  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO unsafe
  FROM (VALUES
    ('preference_candidate'),
    ('preference_candidate_evidence'),
    ('project_knowledge_candidate'),
    ('project_knowledge_candidate_evidence')
  ) AS candidate_table(table_name)
  WHERE NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'SELECT'
    )
    OR NOT has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'INSERT'
    )
    OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'UPDATE'
    )
    OR has_table_privilege(
      'brains_app', format('memory.%I', table_name), 'DELETE'
    );

  IF unsafe IS NOT NULL THEN
    RAISE EXCEPTION 'candidate staging grants are unsafe: %', unsafe;
  END IF;

  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO unsafe
  FROM (VALUES
    ('preference_candidate_review'),
    ('preference_candidate_review_replacement'),
    ('project_space'),
    ('project_knowledge_candidate_review'),
    ('project_knowledge_candidate_review_replacement'),
    ('project_knowledge_head'),
    ('project_knowledge_revision'),
    ('project_knowledge_revision_evidence'),
    ('project_knowledge_relation')
  ) AS protected_table(table_name)
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
    RAISE EXCEPTION 'review/durable grants are unsafe: %', unsafe;
  END IF;

  IF has_table_privilege('brains_app', 'memory.user_preference', 'INSERT')
     OR has_table_privilege('brains_app', 'memory.user_preference', 'UPDATE')
     OR has_table_privilege('brains_app', 'memory.user_preference', 'DELETE')
     OR NOT has_table_privilege('brains_app', 'memory.user_preference', 'SELECT') THEN
    RAISE EXCEPTION 'active preference store is not application read-only';
  END IF;

  SELECT owner.rolname, proc.prosecdef, proc.proconfig
  INTO function_owner, function_security_definer, function_config
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
  JOIN pg_roles AS owner ON owner.oid = proc.proowner
  WHERE namespace.nspname = 'memory'
    AND proc.proname = 'guard_specialized_active_evidence';

  IF function_owner <> 'memory_evidence_maintainer'
     OR NOT function_security_definer
     OR function_config IS DISTINCT FROM ARRAY['search_path=pg_catalog']::text[] THEN
    RAISE EXCEPTION 'specialized evidence guard ownership/configuration is unsafe';
  END IF;

  SELECT owner.rolname, proc.prosecdef, proc.proconfig
  INTO function_owner, function_security_definer, function_config
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
  JOIN pg_roles AS owner ON owner.oid = proc.proowner
  WHERE namespace.nspname = 'memory'
    AND proc.proname = 'populate_specialized_evidence_link_counts';

  IF function_owner <> 'memory_evidence_maintainer'
     OR NOT function_security_definer
     OR function_config IS DISTINCT FROM ARRAY['search_path=pg_catalog']::text[] THEN
    RAISE EXCEPTION 'specialized lifecycle counter ownership/configuration is unsafe';
  END IF;

  SELECT * INTO review_maintainer
  FROM pg_roles
  WHERE rolname = 'memory_review_maintainer';

  IF NOT FOUND
     OR review_maintainer.rolcanlogin
     OR review_maintainer.rolsuper
     OR review_maintainer.rolbypassrls
     OR review_maintainer.rolinherit THEN
    RAISE EXCEPTION 'memory_review_maintainer role is not locked down';
  END IF;

  SELECT owner.rolname, proc.prosecdef, proc.proconfig
  INTO function_owner, function_security_definer, function_config
  FROM pg_proc AS proc
  JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
  JOIN pg_roles AS owner ON owner.oid = proc.proowner
  WHERE namespace.nspname = 'memory'
    AND proc.proname = 'guard_project_revision_insert';

  IF function_owner <> 'memory_review_maintainer'
     OR NOT function_security_definer
     OR function_config IS DISTINCT FROM ARRAY['search_path=pg_catalog']::text[] THEN
    RAISE EXCEPTION 'project revision guard ownership/configuration is unsafe';
  END IF;

  IF has_function_privilege(
       'brains_app', 'memory.guard_preference_review_insert()', 'EXECUTE'
     )
     OR has_function_privilege(
       'brains_app', 'memory.guard_project_revision_insert()', 'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can directly execute internal review guards';
  END IF;
END
$$;

BEGIN;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.project_space(
  project_id, owner_user_id, project_key, display_name, metadata
) VALUES (
  'f4000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'verbal-sage',
  'Verbal Sage',
  '{"test":true}'::jsonb
);

SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, status
) VALUES
  (
    'f1000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'user_statement',
    'memory_v1_specialized_schema_test',
    'active-source',
    'Use direct, concise responses and keep project statements versioned.',
    repeat('1', 64),
    'active'
  ),
  (
    'f1000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'user_statement',
    'memory_v1_specialized_schema_test',
    'quarantined-source',
    'Unreviewed source.',
    repeat('2', 64),
    'quarantined'
  );

INSERT INTO memory.preference_candidate(
  candidate_id, owner_user_id, preference_class, preference_domain,
  preference_key, value, polarity, explicit, stability, surface_policy,
  extraction_confidence, sensitivity, candidate_hash, extractor,
  extractor_version
) VALUES
  (
    'f2000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'response',
    'response_style',
    'response_style.concise',
    '{"enabled":true}'::jsonb,
    'prefer',
    true,
    'stable',
    'silent_style_influence',
    0.990,
    'low',
    repeat('a', 64),
    'memory_v1_test',
    '1.0.0'
  ),
  (
    'f2000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'response',
    'response_style',
    'response_style.dense',
    '{"enabled":true}'::jsonb,
    'prefer',
    true,
    'stable',
    'silent_style_influence',
    0.990,
    'low',
    repeat('b', 64),
    'memory_v1_test',
    '1.0.0'
  );

INSERT INTO memory.preference_candidate_evidence(
  owner_user_id, candidate_id, evidence_id, stance, relevance
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'f2000000-0000-4000-8000-000000000001',
  'f1000000-0000-4000-8000-000000000001',
  'supports',
  1.000
);

INSERT INTO memory.project_knowledge_candidate(
  candidate_id, owner_user_id, project_id, knowledge_kind, knowledge_key,
  canonical_text, document_state, authority_level, authority_source,
  effective_at, extraction_confidence, sensitivity, candidate_hash,
  extractor, extractor_version
) VALUES
  (
    'f5000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'architecture',
    'memory.owner_boundary',
    'Memory ownership is the authenticated Supabase user UUID.',
    'ratified',
    'user_ratified',
    '{"source":"review"}'::jsonb,
    '2026-07-13T00:00:00Z',
    0.990,
    'medium',
    repeat('c', 64),
    'memory_v1_test',
    '1.0.0'
  ),
  (
    'f5000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'requirement',
    'memory.review_path',
    'Candidate review requires a controlled append-only path.',
    'proposed',
    'user_reported',
    '{"source":"review"}'::jsonb,
    '2026-07-13T00:00:00Z',
    0.980,
    'medium',
    repeat('d', 64),
    'memory_v1_test',
    '1.0.0'
  ),
  (
    'f5000000-0000-4000-8000-000000000003',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'requirement',
    'memory.review_path.rewritten',
    'Only a controlled reviewer may append review decisions.',
    'ratified',
    'user_ratified',
    '{"source":"review"}'::jsonb,
    '2026-07-13T00:00:00Z',
    0.990,
    'medium',
    repeat('e', 64),
    'memory_v1_test',
    '1.0.0'
  );

INSERT INTO memory.project_knowledge_candidate_evidence(
  owner_user_id, project_id, candidate_id, evidence_id, stance, relevance
)
SELECT
  '11111111-1111-4111-8111-111111111111'::uuid,
  'f4000000-0000-4000-8000-000000000001'::uuid,
  candidate_id,
  'f1000000-0000-4000-8000-000000000001'::uuid,
  'supports'::memory.evidence_stance,
  1.000
FROM memory.project_knowledge_candidate;

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.preference_candidate_evidence(
      owner_user_id, candidate_id, evidence_id
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'f2000000-0000-4000-8000-000000000002',
      'f1000000-0000-4000-8000-000000000002'
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'quarantined evidence was linked to a preference candidate';
  END IF;

  blocked := false;
  BEGIN
    UPDATE memory.preference_candidate
    SET preference_key = 'mutated'
    WHERE candidate_id = 'f2000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app updated an immutable preference candidate';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.preference_candidate_review(
      owner_user_id, candidate_id, review_number, decision,
      expected_candidate_hash, reviewer_type, rationale, reason_codes
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'f2000000-0000-4000-8000-000000000001',
      1,
      'accept',
      repeat('a', 64),
      'user',
      'Direct review must not be available to the application.',
      ARRAY['test']
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app appended a preference review';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.project_space(
      owner_user_id, project_key, display_name
    ) VALUES (
      '11111111-1111-4111-8111-111111111111', 'unauthorized', 'Unauthorized'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app created a project space';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.user_preference(
      owner_user_id, preference_key, value
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'unauthorized',
      'true'::jsonb
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'brains_app directly mutated active preferences';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.project_knowledge_candidate(
      owner_user_id, project_id, knowledge_kind, knowledge_key,
      canonical_text, document_state, authority_level,
      extraction_confidence, candidate_hash, extractor, extractor_version
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'f4000000-0000-4000-8000-000000000001',
      'status',
      'status.without.time',
      'This invalid status omits its effective time.',
      'unverified',
      'unverified',
      0.500,
      repeat('f', 64),
      'memory_v1_test',
      '1.0.0'
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'status candidate omitted effective_at';
  END IF;
END
$$;

RESET ROLE;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

SET LOCAL ROLE memory_review_maintainer;
SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.evidence
    SET content = 'The row-lock privilege must not permit mutation.'
    WHERE evidence_id = 'f1000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'memory_review_maintainer directly updated evidence';
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
    INSERT INTO memory.preference_candidate_review(
      review_id, owner_user_id, candidate_id, review_number, decision,
      expected_candidate_hash, reviewer_type, rationale, reason_codes
    ) VALUES (
      'f3000000-0000-4000-8000-000000000099',
      '11111111-1111-4111-8111-111111111111',
      'f2000000-0000-4000-8000-000000000001',
      1,
      'rewrite',
      repeat('0', 64),
      'user',
      'Hash mismatch test.',
      ARRAY['hash_mismatch_test']
    );
  EXCEPTION WHEN foreign_key_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'review accepted a stale candidate hash';
  END IF;

  blocked := false;
  BEGIN
    INSERT INTO memory.preference_candidate_review(
      review_id, owner_user_id, candidate_id, review_number, decision,
      expected_candidate_hash, reviewer_type, rationale, reason_codes
    ) VALUES (
      'f3000000-0000-4000-8000-000000000098',
      '11111111-1111-4111-8111-111111111111',
      'f2000000-0000-4000-8000-000000000002',
      1,
      'accept',
      repeat('b', 64),
      'user',
      'Active evidence is required.',
      ARRAY['missing_active_evidence_test']
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'review was appended without active evidence';
  END IF;
END
$$;

INSERT INTO memory.preference_candidate_review(
  review_id, owner_user_id, candidate_id, review_number, decision,
  expected_candidate_hash, reviewer_type, reviewer_ref, rationale, reason_codes
) VALUES (
  'f3000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'f2000000-0000-4000-8000-000000000001',
  1,
  'rewrite',
  repeat('a', 64),
  'user',
  'schema-test',
  'The normalized key should express the denser response preference.',
  ARRAY['canonical_rewrite']
);

INSERT INTO memory.preference_candidate_review_replacement(
  owner_user_id, review_id, replacement_candidate_id, ordinal
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'f3000000-0000-4000-8000-000000000001',
  'f2000000-0000-4000-8000-000000000002',
  1
);

INSERT INTO memory.project_knowledge_candidate_review(
  review_id, owner_user_id, project_id, candidate_id, review_number,
  decision, expected_candidate_hash, reviewer_type, reviewer_ref,
  rationale, reason_codes
) VALUES
  (
    'f6000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'f5000000-0000-4000-8000-000000000001',
    1,
    'accept',
    repeat('c', 64),
    'user',
    'schema-test',
    'The owner boundary is explicitly ratified.',
    ARRAY['user_ratified']
  ),
  (
    'f6000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'f5000000-0000-4000-8000-000000000002',
    1,
    'rewrite',
    repeat('d', 64),
    'user',
    'schema-test',
    'Replace the broad requirement with the exact review authority boundary.',
    ARRAY['canonical_rewrite']
  );

INSERT INTO memory.project_knowledge_candidate_review_replacement(
  owner_user_id, project_id, review_id, replacement_candidate_id, ordinal
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'f4000000-0000-4000-8000-000000000001',
  'f6000000-0000-4000-8000-000000000002',
  'f5000000-0000-4000-8000-000000000003',
  1
);

INSERT INTO memory.project_knowledge_head(
  knowledge_id, owner_user_id, project_id, knowledge_key, knowledge_kind
) VALUES
  (
    'f7000000-0000-4000-8000-000000000001',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'memory.owner_boundary',
    'architecture'
  ),
  (
    'f7000000-0000-4000-8000-000000000002',
    '11111111-1111-4111-8111-111111111111',
    'f4000000-0000-4000-8000-000000000001',
    'memory.review_path',
    'requirement'
  );

INSERT INTO memory.project_knowledge_revision(
  revision_id, owner_user_id, project_id, knowledge_id, revision_number,
  canonical_text, content_sha256, document_state, authority_level,
  authority_source, effective_from, accepted_review_id, sensitivity
) VALUES (
  'f8000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'f4000000-0000-4000-8000-000000000001',
  'f7000000-0000-4000-8000-000000000001',
  1,
  'Memory ownership is the authenticated Supabase user UUID.',
  repeat('c', 64),
  'ratified',
  'user_ratified',
  '{"source":"review"}'::jsonb,
  '2026-07-13T00:00:00Z',
  'f6000000-0000-4000-8000-000000000001',
  'medium'
);

INSERT INTO memory.project_knowledge_revision_evidence(
  owner_user_id, project_id, revision_id, evidence_id, stance, relevance
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'f4000000-0000-4000-8000-000000000001',
  'f8000000-0000-4000-8000-000000000001',
  'f1000000-0000-4000-8000-000000000001',
  'supports',
  1.000
);

INSERT INTO memory.project_knowledge_relation(
  owner_user_id, project_id, from_knowledge_id, to_knowledge_id,
  relation_type, rationale
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'f4000000-0000-4000-8000-000000000001',
  'f7000000-0000-4000-8000-000000000002',
  'f7000000-0000-4000-8000-000000000001',
  'depends_on',
  'The controlled review path depends on strict owner isolation.'
);

DO $$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.project_space
    SET display_name = 'Mutated'
    WHERE project_id = 'f4000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'project_space was mutable';
  END IF;

  blocked := false;
  BEGIN
    DELETE FROM memory.project_knowledge_candidate_review
    WHERE review_id = 'f6000000-0000-4000-8000-000000000001';
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'project review event was deleted';
  END IF;
END
$$;

SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '22222222-2222-4222-8222-222222222222', true);

DO $$
DECLARE
  visible integer;
  blocked boolean := false;
BEGIN
  SELECT
    (SELECT count(*) FROM memory.preference_candidate)
    + (SELECT count(*) FROM memory.preference_candidate_review)
    + (SELECT count(*) FROM memory.project_space)
    + (SELECT count(*) FROM memory.project_knowledge_candidate)
    + (SELECT count(*) FROM memory.project_knowledge_candidate_review)
    + (SELECT count(*) FROM memory.project_knowledge_head)
    + (SELECT count(*) FROM memory.project_knowledge_revision)
  INTO visible;

  IF visible <> 0 THEN
    RAISE EXCEPTION 'actor B can see actor A specialized memory rows';
  END IF;

  BEGIN
    INSERT INTO memory.preference_candidate(
      owner_user_id, preference_class, preference_domain, preference_key,
      value, polarity, stability, surface_policy, extraction_confidence,
      candidate_hash, extractor, extractor_version
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'response',
      'response_style',
      'cross_owner',
      'true'::jsonb,
      'prefer',
      'stable',
      'silent_style_influence',
      0.900,
      repeat('9', 64),
      'memory_v1_test',
      '1.0.0'
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B inserted an actor A preference candidate';
  END IF;
END
$$;

SELECT set_config('app.user_id', '11111111-1111-4111-8111-111111111111', true);

SELECT event_id
FROM memory.transition_evidence_lifecycle(
  'f1000000-0000-4000-8000-000000000001',
  'f9000000-0000-4000-8000-000000000001',
  'redact_content',
  'administrative_repair',
  'job',
  'specialized-schema-test',
  '{"test":"specialized-link-counts"}'::jsonb
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence_lifecycle_event
    WHERE request_id = 'f9000000-0000-4000-8000-000000000001'
      AND preference_candidate_link_count = 1
      AND project_candidate_link_count = 3
      AND project_revision_link_count = 1
  ) THEN
    RAISE EXCEPTION 'evidence lifecycle audit omitted specialized links';
  END IF;
END
$$;

ROLLBACK;
