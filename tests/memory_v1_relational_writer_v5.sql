\set ON_ERROR_STOP on

DO $metadata$
DECLARE
  relation_name text;
  api regprocedure;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'writer security suite must start as sage';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'memory_v5_writer'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_writer role attributes are unsafe';
  END IF;
  IF pg_has_role('brains_app', 'memory_v5_writer', 'MEMBER') THEN
    RAISE EXCEPTION 'brains_app must not be a member of memory_v5_writer';
  END IF;

  FOREACH relation_name IN ARRAY ARRAY[
    'relational_stage_batch',
    'relational_operation_request',
    'entity_mention',
    'entity_resolution_plan',
    'entity_resolution_candidate',
    'entity_resolution_review',
    'entity_resolution_apply',
    'entity_alias_observation',
    'observation',
    'observation_temporal',
    'observation_entity_binding',
    'claim_observation',
    'candidate_observation'
  ]
  LOOP
    IF has_table_privilege(
      'brains_app', format('memory.%I', relation_name), 'SELECT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', relation_name), 'INSERT'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', relation_name), 'UPDATE'
    ) OR has_table_privilege(
      'brains_app', format('memory.%I', relation_name), 'DELETE'
    ) THEN
      RAISE EXCEPTION 'brains_app has direct access to memory.%', relation_name;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'memory.% is missing forced RLS', relation_name;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_policy AS policy
      JOIN pg_class AS relation ON relation.oid = policy.polrelid
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND policy.polname = 'owner_isolation'
        AND policy.polroles = ARRAY['memory_v5_writer'::regrole::oid]
    ) THEN
      RAISE EXCEPTION 'memory.% policy is not restricted to the writer role', relation_name;
    END IF;
  END LOOP;

  FOREACH api IN ARRAY ARRAY[
    'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'::regprocedure,
    'memory.preflight_entity_resolution_review_v5(uuid,memory.entity_review_decision,text)'::regprocedure,
    'memory.review_entity_resolution_v5(uuid,uuid,memory.entity_review_decision,text,text)'::regprocedure,
    'memory.preflight_entity_resolution_apply_v5(uuid,uuid)'::regprocedure,
    'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'::regprocedure
  ]
  LOOP
    IF NOT has_function_privilege('brains_app', api, 'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app cannot execute required API %', api;
    END IF;
    IF EXISTS (
      SELECT 1
      FROM pg_proc AS procedure
      WHERE procedure.oid = api
        AND (
          NOT procedure.prosecdef
          OR procedure.proowner <> 'memory_v5_writer'::regrole::oid
          OR array_to_string(procedure.proconfig, ',') NOT LIKE '%search_path=%'
        )
    ) THEN
      RAISE EXCEPTION 'API % is not a search-path-locked writer definer', api;
    END IF;
    IF EXISTS (
      SELECT 1 FROM pg_proc AS procedure,
        LATERAL aclexplode(COALESCE(procedure.proacl, acldefault('f', procedure.proowner))) AS acl
      WHERE procedure.oid = api
        AND acl.grantee = 0
        AND acl.privilege_type = 'EXECUTE'
    ) THEN
      RAISE EXCEPTION 'PUBLIC can execute API %', api;
    END IF;
  END LOOP;

  IF has_function_privilege(
    'brains_app', 'memory.require_v5_writer_context()', 'EXECUTE'
  ) OR has_function_privilege(
    'brains_app', 'memory.v5_digest_text(text)', 'EXECUTE'
  ) OR has_function_privilege(
    'brains_app', 'memory.normalize_entity_name_v5(text)', 'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app can execute an internal writer helper';
  END IF;
END
$metadata$;

BEGIN;

INSERT INTO memory.entity(
  entity_id, owner_user_id, entity_key, entity_type,
  canonical_name, normalized_name
) VALUES
  (
    'a1111111-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'self', 'self', 'Owner A', 'owner a'
  ),
  (
    'a1111111-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111',
    'animal:koda:1', 'animal', 'Koda', 'koda'
  ),
  (
    'a1111111-1111-4111-8111-111111111113',
    '11111111-1111-4111-8111-111111111111',
    'animal:rex:1', 'animal', 'Rex', 'rex'
  ),
  (
    'b1111111-1111-4111-8111-111111111111',
    '22222222-2222-4222-8222-222222222222',
    'self', 'self', 'Owner B', 'owner b'
  );

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, observed_at, sensitivity
) VALUES
  (
    'aeeeeeee-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'user_statement', 'public.chat_log', 'chat-a-1',
    'I have a dog named Koda. Mira is my friend. Rex is nearby. My caregiver helped.',
    repeat('a', 64), '2026-07-15T12:00:00Z', 'medium'
  ),
  (
    'beeeeeee-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'user_statement', 'public.chat_log', 'chat-b-1',
    'Owner B evidence.', repeat('b', 64),
    '2026-07-15T12:01:00Z', 'medium'
  );

CREATE FUNCTION pg_temp.assert_direct_write_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.relational_operation_request(
      owner_user_id, request_id, operation, target_key,
      manifest_sha256, outcome, result, invoked_by_session
    ) VALUES (
      '11111111-1111-4111-8111-111111111111', gen_random_uuid(),
      'stage_packet', 'forbidden', repeat('f', 64),
      'applied', '{}'::jsonb, session_user
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app directly wrote a writer table';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_preflight_hidden(p_resolution_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  hidden boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      p_resolution_id, NULL
    );
  EXCEPTION WHEN no_data_found THEN
    hidden := true;
  END;
  IF NOT hidden THEN
    RAISE EXCEPTION 'cross-owner resolution was not hidden';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', NULL
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'writer API accepted a missing actor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_stage_replay_mismatch(
  p_evidence_id uuid,
  p_extraction text,
  p_resolution text,
  p_extraction_sha text,
  p_resolution_sha text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.stage_relational_packet_v5(
      '10000000-0000-4000-8000-000000000001',
      p_evidence_id, 'different_extractor', 'v5',
      p_extraction, p_resolution, p_extraction_sha, p_resolution_sha
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'request replay accepted a changed extractor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_ambiguous_apply_blocked(
  p_resolution_id uuid,
  p_manifest text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.apply_entity_resolution_v5(
      '30000000-0000-4000-8000-000000000004',
      p_resolution_id, NULL, p_manifest
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'auto-link accepted an ambiguous owner entity set';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_role_create_blocked(
  p_resolution_id uuid,
  p_review_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5(
      p_resolution_id, p_review_id
    );
  EXCEPTION WHEN feature_not_supported THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'role-only entity creation was not blocked';
  END IF;
END
$function$;

SELECT jsonb_build_object(
  'contract_version', 'memory_v1_relational_extraction_v5',
  'source_envelope', jsonb_build_object(
    'job_id', 'writer-test-a',
    'source_system', 'public.chat_log',
    'source_external_id', 'chat-a-1',
    'source_sha256', repeat('a', 64),
    'source_recorded_at', '2026-07-15T12:00:00Z'
  ),
  'predicate_registry_version', 'memory_predicate_registry_v5',
  'entity_mentions', jsonb_build_array(
    jsonb_build_object(
      'entity_ref', 'e01', 'entity_type', 'self',
      'mention_kind', 'self_reference', 'name_text', NULL,
      'relationship_role', NULL,
      'source_spans', jsonb_build_array(jsonb_build_object(
        'start', 0, 'end', 1, 'span_sha256', repeat('1', 64)
      )),
      'extraction_confidence', 1.0, 'reason_codes', '[]'::jsonb
    ),
    jsonb_build_object(
      'entity_ref', 'e02', 'entity_type', 'animal',
      'mention_kind', 'named', 'name_text', 'Koda',
      'relationship_role', 'pet',
      'source_spans', jsonb_build_array(jsonb_build_object(
        'start', 19, 'end', 23, 'span_sha256', repeat('2', 64)
      )),
      'extraction_confidence', 0.99, 'reason_codes', '[]'::jsonb
    ),
    jsonb_build_object(
      'entity_ref', 'e03', 'entity_type', 'person',
      'mention_kind', 'named', 'name_text', 'Mira',
      'relationship_role', 'friend',
      'source_spans', jsonb_build_array(jsonb_build_object(
        'start', 25, 'end', 29, 'span_sha256', repeat('3', 64)
      )),
      'extraction_confidence', 0.97, 'reason_codes', '[]'::jsonb
    ),
    jsonb_build_object(
      'entity_ref', 'e04', 'entity_type', 'animal',
      'mention_kind', 'named', 'name_text', 'Rex',
      'relationship_role', 'pet',
      'source_spans', jsonb_build_array(jsonb_build_object(
        'start', 44, 'end', 47, 'span_sha256', repeat('4', 64)
      )),
      'extraction_confidence', 0.95, 'reason_codes', '[]'::jsonb
    ),
    jsonb_build_object(
      'entity_ref', 'e05', 'entity_type', 'person',
      'mention_kind', 'role_only', 'name_text', NULL,
      'relationship_role', 'caregiver',
      'source_spans', jsonb_build_array(jsonb_build_object(
        'start', 63, 'end', 72, 'span_sha256', repeat('5', 64)
      )),
      'extraction_confidence', 0.90, 'reason_codes', '[]'::jsonb
    )
  ),
  'observations', jsonb_build_array(jsonb_build_object(
    'observation_ref', 'o01', 'subject_entity_ref', 'e01',
    'predicate', 'relationship.has_pet',
    'predicate_registry_status', 'governed',
    'object', jsonb_build_object('kind', 'entity', 'entity_ref', 'e02'),
    'polarity', 'affirmed', 'modality', 'asserted',
    'projection_class', 'direct_claim',
    'surface_policy', 'direct_or_relevant',
    'temporal', jsonb_build_object(
      'semantic', 'none', 'shape', 'none', 'basis', 'none',
      'source_form', 'none', 'certainty', 'unknown', 'precision', 'unknown',
      'instant', NULL, 'calendar_range', NULL, 'instant_range', NULL,
      'relative_offset', NULL, 'recurrence', NULL,
      'anchored_to_source_time', false,
      'normalization_policy_version', 'memory_temporal_normalization_v5',
      'reason_codes', '[]'::jsonb
    ),
    'project_scope', jsonb_build_object(
      'state', 'not_applicable', 'project_key', NULL,
      'binding_source', 'not_applicable'
    ),
    'sensitivity', 'medium', 'extraction_confidence', 0.99,
    'source_spans', jsonb_build_array(jsonb_build_object(
      'start', 0, 'end', 23, 'span_sha256', repeat('6', 64)
    )),
    'reason_codes', '[]'::jsonb
  )),
  'comparison_hints', '[]'::jsonb,
  'deferrals', '[]'::jsonb,
  'packet_findings', '[]'::jsonb
)::text AS extraction_packet
\gset

SELECT jsonb_build_object(
  'contract_version', 'memory_v1_entity_resolution_review_v5',
  'source_envelope', jsonb_build_object(
    'job_id', 'writer-test-a',
    'source_system', 'public.chat_log',
    'source_external_id', 'chat-a-1',
    'source_sha256', repeat('a', 64),
    'source_recorded_at', '2026-07-15T12:00:00Z'
  ),
  'predicate_registry_version', 'memory_predicate_registry_v5',
  'entity_normalization_version', 'memory_entity_normalization_v5',
  'resolver', 'writer_test_resolver',
  'resolver_version', 'v5',
  'resolutions', jsonb_build_array(
    jsonb_build_object(
      'entity_ref', 'e01', 'mention_sha256', repeat('a', 64),
      'action', 'link_existing', 'decision_state', 'auto_link_eligible',
      'selected_entity_id', 'a1111111-1111-4111-8111-111111111111',
      'proposed_entity', NULL,
      'candidate_set_sha256', repeat('b', 64),
      'candidate_set', jsonb_build_array(jsonb_build_object(
        'entity_id', 'a1111111-1111-4111-8111-111111111111',
        'entity_type', 'self',
        'features', jsonb_build_object(
          'active_status', true, 'entity_type_match', true,
          'exact_canonical_name', false, 'exact_alias', false,
          'relationship_role_supported', false,
          'source_local_coreference', true, 'graph_neighbor_supported', true,
          'conflicting_attribute_count', 0, 'same_name_candidate_count', 1
        ),
        'exclusion_reasons', '[]'::jsonb
      )),
      'review_reason_codes', '[]'::jsonb,
      'decision_sha256', repeat('c', 64)
    ),
    jsonb_build_object(
      'entity_ref', 'e02', 'mention_sha256', repeat('d', 64),
      'action', 'link_existing', 'decision_state', 'auto_link_eligible',
      'selected_entity_id', 'a1111111-1111-4111-8111-111111111112',
      'proposed_entity', NULL,
      'candidate_set_sha256', repeat('e', 64),
      'candidate_set', jsonb_build_array(jsonb_build_object(
        'entity_id', 'a1111111-1111-4111-8111-111111111112',
        'entity_type', 'animal',
        'features', jsonb_build_object(
          'active_status', true, 'entity_type_match', true,
          'exact_canonical_name', true, 'exact_alias', false,
          'relationship_role_supported', true,
          'source_local_coreference', false, 'graph_neighbor_supported', false,
          'conflicting_attribute_count', 0, 'same_name_candidate_count', 1
        ),
        'exclusion_reasons', '[]'::jsonb
      )),
      'review_reason_codes', '[]'::jsonb,
      'decision_sha256', repeat('f', 64)
    ),
    jsonb_build_object(
      'entity_ref', 'e03', 'mention_sha256', repeat('1', 64),
      'action', 'create_new', 'decision_state', 'manual_review_required',
      'selected_entity_id', NULL,
      'proposed_entity', jsonb_build_object(
        'entity_type', 'person', 'identity_state', 'named',
        'canonical_name', 'Mira', 'display_label', 'Mira',
        'creation_reason', 'new_named_person'
      ),
      'candidate_set_sha256', repeat('2', 64),
      'candidate_set', '[]'::jsonb,
      'review_reason_codes', jsonb_build_array('new_entity_requires_review'),
      'decision_sha256', repeat('3', 64)
    ),
    jsonb_build_object(
      'entity_ref', 'e04', 'mention_sha256', repeat('4', 64),
      'action', 'link_existing', 'decision_state', 'auto_link_eligible',
      'selected_entity_id', 'a1111111-1111-4111-8111-111111111113',
      'proposed_entity', NULL,
      'candidate_set_sha256', repeat('5', 64),
      'candidate_set', jsonb_build_array(jsonb_build_object(
        'entity_id', 'a1111111-1111-4111-8111-111111111113',
        'entity_type', 'animal',
        'features', jsonb_build_object(
          'active_status', true, 'entity_type_match', true,
          'exact_canonical_name', true, 'exact_alias', false,
          'relationship_role_supported', true,
          'source_local_coreference', false, 'graph_neighbor_supported', false,
          'conflicting_attribute_count', 0, 'same_name_candidate_count', 1
        ),
        'exclusion_reasons', '[]'::jsonb
      )),
      'review_reason_codes', '[]'::jsonb,
      'decision_sha256', repeat('6', 64)
    ),
    jsonb_build_object(
      'entity_ref', 'e05', 'mention_sha256', repeat('7', 64),
      'action', 'create_new', 'decision_state', 'manual_review_required',
      'selected_entity_id', NULL,
      'proposed_entity', jsonb_build_object(
        'entity_type', 'person', 'identity_state', 'role_only',
        'canonical_name', NULL, 'display_label', 'caregiver',
        'creation_reason', 'unresolved_role_only_person'
      ),
      'candidate_set_sha256', repeat('8', 64),
      'candidate_set', '[]'::jsonb,
      'review_reason_codes', jsonb_build_array('role_only_creation_blocked'),
      'decision_sha256', repeat('9', 64)
    )
  ),
  'packet_sha256', repeat('0', 64)
)::text AS resolution_packet
\gset

SELECT encode(digest(convert_to(:'extraction_packet', 'UTF8'), 'sha256'), 'hex')
  AS extraction_sha,
  encode(digest(convert_to(:'resolution_packet', 'UTF8'), 'sha256'), 'hex')
  AS resolution_sha
\gset

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', '', true);
SELECT pg_temp.assert_missing_actor_denied();
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
SELECT pg_temp.assert_direct_write_denied();

SELECT * FROM memory.stage_relational_packet_v5(
  '10000000-0000-4000-8000-000000000001',
  'aeeeeeee-1111-4111-8111-111111111111',
  'writer_test_extractor', 'v5',
  :'extraction_packet', :'resolution_packet',
  :'extraction_sha', :'resolution_sha'
)
\gset stage_

SELECT 1 / ((:'stage_outcome' = 'applied')::integer);
SELECT 1 / ((:'stage_mentions_inserted'::integer = 5)::integer);
SELECT 1 / ((:'stage_resolutions_inserted'::integer = 5)::integer);
SELECT 1 / ((:'stage_candidates_inserted'::integer = 3)::integer);
SELECT 1 / ((:'stage_observations_inserted'::integer = 1)::integer);
SELECT 1 / ((:'stage_temporals_inserted'::integer = 1)::integer);

SELECT (:'stage_result'::jsonb->'resolution_ids'->>'e01')::uuid
  AS self_resolution,
  (:'stage_result'::jsonb->'resolution_ids'->>'e02')::uuid
  AS koda_resolution,
  (:'stage_result'::jsonb->'resolution_ids'->>'e03')::uuid
  AS mira_resolution,
  (:'stage_result'::jsonb->'resolution_ids'->>'e04')::uuid
  AS rex_resolution,
  (:'stage_result'::jsonb->'resolution_ids'->>'e05')::uuid
  AS role_resolution
\gset

SELECT * FROM memory.stage_relational_packet_v5(
  '10000000-0000-4000-8000-000000000001',
  'aeeeeeee-1111-4111-8111-111111111111',
  'writer_test_extractor', 'v5',
  :'extraction_packet', :'resolution_packet',
  :'extraction_sha', :'resolution_sha'
)
\gset replay_

SELECT 1 / ((:'replay_outcome' = 'replayed')::integer);
SELECT 1 / ((
  :'replay_mentions_inserted'::integer
  + :'replay_resolutions_inserted'::integer
  + :'replay_candidates_inserted'::integer
  + :'replay_observations_inserted'::integer
  + :'replay_temporals_inserted'::integer = 0
)::integer);

SELECT * FROM memory.stage_relational_packet_v5(
  '10000000-0000-4000-8000-000000000002',
  'aeeeeeee-1111-4111-8111-111111111111',
  'writer_test_extractor', 'v5',
  :'extraction_packet', :'resolution_packet',
  :'extraction_sha', :'resolution_sha'
)
\gset semantic_replay_

SELECT 1 / ((:'semantic_replay_outcome' = 'replayed')::integer);
SELECT pg_temp.assert_stage_replay_mismatch(
  'aeeeeeee-1111-4111-8111-111111111111',
  :'extraction_packet', :'resolution_packet',
  :'extraction_sha', :'resolution_sha'
);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  :'self_resolution', NULL
)
\gset self_preflight_
SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000001',
  :'self_resolution', NULL, :'self_preflight_apply_manifest_sha256'
)
\gset self_apply_
SELECT 1 / ((:'self_apply_outcome' = 'applied')::integer);
SELECT 1 / ((:'self_apply_bindings_created'::integer = 0)::integer);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  :'koda_resolution', NULL
)
\gset koda_preflight_
SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000002',
  :'koda_resolution', NULL, :'koda_preflight_apply_manifest_sha256'
)
\gset koda_apply_
SELECT 1 / ((:'koda_apply_outcome' = 'applied')::integer);
SELECT 1 / ((:'koda_apply_bindings_created'::integer = 1)::integer);

SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000002',
  :'koda_resolution', NULL, :'koda_preflight_apply_manifest_sha256'
)
\gset koda_replay_
SELECT 1 / ((:'koda_replay_outcome' = 'replayed')::integer);
SELECT 1 / ((:'koda_replay_bindings_created'::integer = 0)::integer);

SELECT * FROM memory.preflight_entity_resolution_review_v5(
  :'mira_resolution', 'approved', 'Mira is a distinct named person.'
)
\gset mira_review_preflight_
SELECT * FROM memory.review_entity_resolution_v5(
  '20000000-0000-4000-8000-000000000001',
  :'mira_resolution', 'approved', 'Mira is a distinct named person.',
  :'mira_review_preflight_authorization_manifest_sha256'
)
\gset mira_review_
SELECT 1 / ((:'mira_review_outcome' = 'applied')::integer);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  :'mira_resolution', :'mira_review_review_id'
)
\gset mira_apply_preflight_
SELECT * FROM memory.apply_entity_resolution_v5(
  '30000000-0000-4000-8000-000000000003',
  :'mira_resolution', :'mira_review_review_id',
  :'mira_apply_preflight_apply_manifest_sha256'
)
\gset mira_apply_
SELECT 1 / ((:'mira_apply_outcome' = 'applied')::integer);

SELECT * FROM memory.preflight_entity_resolution_review_v5(
  :'role_resolution', 'approved', 'Review recorded; creation must remain blocked.'
)
\gset role_review_preflight_
SELECT * FROM memory.review_entity_resolution_v5(
  '20000000-0000-4000-8000-000000000002',
  :'role_resolution', 'approved',
  'Review recorded; creation must remain blocked.',
  :'role_review_preflight_authorization_manifest_sha256'
)
\gset role_review_
SELECT pg_temp.assert_role_create_blocked(
  :'role_resolution', :'role_review_review_id'
);

SELECT * FROM memory.preflight_entity_resolution_apply_v5(
  :'rex_resolution', NULL
)
\gset rex_preflight_

RESET SESSION AUTHORIZATION;
INSERT INTO memory.entity(
  entity_id, owner_user_id, entity_key, entity_type,
  canonical_name, normalized_name
) VALUES (
  'a1111111-1111-4111-8111-111111111114',
  '11111111-1111-4111-8111-111111111111',
  'animal:rex:2', 'animal', 'Rex', 'rex'
);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
SELECT pg_temp.assert_ambiguous_apply_blocked(
  :'rex_resolution', :'rex_preflight_apply_manifest_sha256'
);

SELECT set_config(
  'app.user_id', '22222222-2222-4222-8222-222222222222', true
);
SELECT pg_temp.assert_preflight_hidden(:'self_resolution');

RESET SESSION AUTHORIZATION;

DO $results$
BEGIN
  IF (SELECT count(*) FROM memory.relational_stage_batch) <> 1
     OR (SELECT count(*) FROM memory.relational_operation_request) <> 6
     OR (SELECT count(*) FROM memory.entity_mention) <> 5
     OR (SELECT count(*) FROM memory.entity_resolution_plan) <> 5
     OR (SELECT count(*) FROM memory.entity_resolution_candidate) <> 3
     OR (SELECT count(*) FROM memory.entity_resolution_review) <> 2
     OR (SELECT count(*) FROM memory.entity_resolution_apply) <> 3
     OR (SELECT count(*) FROM memory.entity_alias_observation) <> 2
     OR (SELECT count(*) FROM memory.observation) <> 1
     OR (SELECT count(*) FROM memory.observation_temporal) <> 1
     OR (SELECT count(*) FROM memory.observation_entity_binding) <> 1 THEN
    RAISE EXCEPTION 'writer transaction produced unexpected row counts';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.entity_mention
    WHERE owner_user_id = '22222222-2222-4222-8222-222222222222'
  ) OR EXISTS (
    SELECT 1 FROM memory.relational_operation_request
    WHERE owner_user_id = '22222222-2222-4222-8222-222222222222'
  ) THEN
    RAISE EXCEPTION 'writer transaction crossed owner boundaries';
  END IF;
  IF (
    SELECT count(*) FROM memory.entity
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND canonical_name = 'Mira'
      AND metadata->>'origin' = 'memory_v1_relational_v5'
  ) <> 1 THEN
    RAISE EXCEPTION 'reviewed named entity was not created exactly once';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.entity_resolution_apply AS applied
    JOIN memory.entity_resolution_plan AS plan
      ON plan.owner_user_id = applied.owner_user_id
     AND plan.resolution_id = applied.resolution_id
    JOIN memory.entity_mention AS mention
      ON mention.owner_user_id = plan.owner_user_id
     AND mention.mention_id = plan.mention_id
    WHERE mention.mention_kind = 'role_only'
  ) THEN
    RAISE EXCEPTION 'role-only resolution was applied';
  END IF;
END
$results$;

ROLLBACK;

DO $zero_write$
BEGIN
  IF EXISTS (SELECT 1 FROM memory.relational_stage_batch)
     OR EXISTS (SELECT 1 FROM memory.relational_operation_request)
     OR EXISTS (SELECT 1 FROM memory.entity_mention)
     OR EXISTS (SELECT 1 FROM memory.entity_resolution_plan)
     OR EXISTS (SELECT 1 FROM memory.entity_resolution_review)
     OR EXISTS (SELECT 1 FROM memory.entity_resolution_apply)
     OR EXISTS (SELECT 1 FROM memory.observation)
     OR EXISTS (SELECT 1 FROM memory.entity WHERE entity_key LIKE 'ent_%') THEN
    RAISE EXCEPTION 'rolled-back writer suite left data behind';
  END IF;
END
$zero_write$;

SELECT 'memory_v1_relational_writer_v5: PASS' AS result;
