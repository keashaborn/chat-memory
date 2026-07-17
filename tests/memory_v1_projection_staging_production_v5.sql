\set ON_ERROR_STOP on

DO $metadata$
DECLARE
  relation_name text;
  helper regprocedure;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'projection security suite must start as sage';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = 'memory_v5_writer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'memory_v5_writer role attributes are unsafe';
  END IF;
  IF pg_has_role('brains_app', 'memory_v5_writer', 'MEMBER') THEN
    RAISE EXCEPTION 'brains_app must not be a member of memory_v5_writer';
  END IF;

  FOREACH relation_name IN ARRAY ARRAY[
    'projection_plan',
    'projection_plan_item',
    'projection_claim_payload',
    'projection_preference_payload',
    'projection_project_payload',
    'projection_plan_observation',
    'projection_plan_relation',
    'projection_review',
    'projection_apply_event',
    'preference_revision_observation',
    'project_knowledge_revision_observation',
    'claim_relation_v5',
    'preference_relation_v5',
    'project_knowledge_relation_v5',
    'projection_dispatch_v5'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND relation.relowner = 'sage'::regrole::oid
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'memory.% ownership or forced RLS is unsafe', relation_name;
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
        AND pg_get_expr(policy.polqual, policy.polrelid)
              LIKE '%current_actor_user_id%'
        AND pg_get_expr(policy.polwithcheck, policy.polrelid)
              LIKE '%current_actor_user_id%'
    ) THEN
      RAISE EXCEPTION 'memory.% owner policy is unsafe', relation_name;
    END IF;
    IF has_table_privilege('brains_app', format('memory.%I', relation_name), 'SELECT')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'INSERT')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'UPDATE')
       OR has_table_privilege('brains_app', format('memory.%I', relation_name), 'DELETE') THEN
      RAISE EXCEPTION 'brains_app has direct access to memory.%', relation_name;
    END IF;
    IF NOT has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'SELECT'
       ) OR NOT has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'INSERT'
       ) OR has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'UPDATE'
       ) OR has_table_privilege(
         'memory_v5_writer', format('memory.%I', relation_name), 'DELETE'
       ) THEN
      RAISE EXCEPTION 'memory.% writer grants are not insert-only', relation_name;
    END IF;
  END LOOP;

  IF NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_review_v5(uuid,text,memory.projection_review_decision_v5,text,text,text,jsonb)',
       'EXECUTE'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.review_projection_v5(uuid,text,memory.projection_review_decision_v5,text,text,text,jsonb,text)',
       'EXECUTE'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_apply_v5(uuid,text,uuid)',
       'EXECUTE'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.apply_projection_v5(uuid,uuid,text,uuid,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'projection review/apply API grants are incomplete';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_constraint AS constraint_row
    JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL unnest(constraint_row.conkey)
      WITH ORDINALITY AS source_key(attnum, ordinal)
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum = source_key.attnum
    WHERE namespace.nspname = 'memory'
      AND relation.relname LIKE 'projection_%'
      AND constraint_row.contype = 'f'
      AND constraint_row.confrelid <> 'memory.predicate_contract'::regclass
      AND source_key.ordinal = 1
      AND attribute.attname <> 'owner_user_id'
  ) THEN
    RAISE EXCEPTION 'a projection foreign key is not owner-prefixed';
  END IF;

  FOREACH helper IN ARRAY ARRAY[
    'memory.v5_canonical_json_text(jsonb)'::regprocedure,
    'memory.v5_projection_owner_manifest_sha256(uuid,text)'::regprocedure,
    'memory.v5_projection_semantic_key_sha256(uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,memory.observation_polarity,memory.observation_modality,jsonb)'::regprocedure,
    'memory.guard_projection_actor_v5()'::regprocedure,
    'memory.guard_projection_plan_complete_v5()'::regprocedure,
    'memory.guard_projection_item_complete_v5()'::regprocedure
  ]
  LOOP
    IF EXISTS (
      SELECT 1 FROM pg_proc AS procedure
      WHERE procedure.oid = helper
        AND procedure.proowner <> 'memory_v5_writer'::regrole::oid
    ) OR has_function_privilege('brains_app', helper, 'EXECUTE')
       OR has_function_privilege('public', helper, 'EXECUTE') THEN
      RAISE EXCEPTION 'projection helper % is exposed or misowned', helper;
    END IF;
  END LOOP;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.observation'::regclass
      AND conname = 'observation_owner_id_sha_uq'
      AND contype = 'u'
  ) THEN
    RAISE EXCEPTION 'observation projection identity key is missing';
  END IF;

  IF memory.v5_projection_owner_manifest_sha256(
       '11111111-1111-4111-8111-111111111111', repeat('a', 64)
     ) <> '47b7bac4c4b882a4edc66accc142275d3bd1516a3263e73fe5e506c98d38e694' THEN
    RAISE EXCEPTION 'owner manifest canonical hash differs from Python contract';
  END IF;
  IF memory.v5_projection_semantic_key_sha256(
       '11111111-1111-4111-8111-111111111111', 'claim',
       'a1111111-1111-4111-8111-111111111111', 'identity.name',
       'literal', NULL, repeat('b', 64), 'affirmed', 'asserted', '{}'::jsonb
     ) <> '098948330b26b40ca7371e016aa70d47bc8a7aea51d11a788bd4e7e224f1cad5' THEN
    RAISE EXCEPTION 'semantic identity hash differs from Python contract';
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
    'project:memory-v1', 'project', 'Memory V1', 'memory v1'
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
    'user_statement', 'projection.clone.test', 'owner-a',
    'My name is Eric. Keep responses concise. Memory V1 requires account isolation.',
    repeat('a', 64), '2026-07-15T12:00:00Z', 'medium'
  ),
  (
    'beeeeeee-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'user_statement', 'projection.clone.test', 'owner-b',
    'Owner B evidence.', repeat('b', 64),
    '2026-07-15T12:01:00Z', 'medium'
  );

INSERT INTO memory.project_space(
  project_id, owner_user_id, project_key, display_name
) VALUES
  (
    'a5555555-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'verbal-sage', 'Verbal Sage'
  ),
  (
    'b5555555-2222-4222-8222-222222222222',
    '22222222-2222-4222-8222-222222222222',
    'other-project', 'Other Project'
  );

SET ROLE brains_app;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
SELECT 1 / ((component_key = 'memory-v1')::integer)
FROM memory.apply_owner_project_component_v5(
  'c5555555-1111-4111-8111-111111111111',
  'a5555555-1111-4111-8111-111111111111',
  'memory-v1', 'Memory V1',
  NULL,
  ARRAY['memory v1'],
  '{"test":"projection_component_scope"}'::jsonb
);
SELECT 1 / ((component_key = 'resse')::integer)
FROM memory.apply_owner_project_component_v5(
  'c5555555-1111-4111-8111-111111111112',
  'a5555555-1111-4111-8111-111111111111',
  'resse', 'RESSE',
  NULL,
  ARRAY['resse'],
  '{"test":"projection_sibling_scope"}'::jsonb
);
SELECT set_config(
  'app.user_id', '22222222-2222-4222-8222-222222222222', true
);
SELECT 1 / ((component_key = 'owner-b-only')::integer)
FROM memory.apply_owner_project_component_v5(
  'c5555555-2222-4222-8222-222222222222',
  'b5555555-2222-4222-8222-222222222222',
  'owner-b-only', 'Owner B Only',
  NULL,
  ARRAY['owner b only'],
  '{"test":"projection_cross_owner_scope"}'::jsonb
);
RESET ROLE;

CREATE FUNCTION pg_temp.assert_application_read_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.projection_plan;
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app directly read projection staging';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_actor_insert_denied(p_owner uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.projection_review(
      owner_user_id, plan_id, projection_ref, review_number,
      parent_review_state, decision, expected_projection_sha256,
      expected_semantic_key_sha256, reviewer_type, reason,
      reason_codes, authorization_manifest_sha256
    ) VALUES (
      p_owner, gen_random_uuid(), 'p01', 1,
      'manual_review_required', 'authorized', repeat('1', 64),
      repeat('2', 64), 'system', 'must be denied',
      '[]'::jsonb, repeat('3', 64)
    );
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'projection insert accepted missing or cross-owner actor';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_append_only_denied(p_plan_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  update_denied boolean := false;
  delete_denied boolean := false;
BEGIN
  BEGIN
    UPDATE memory.projection_plan
    SET projector_version = 'mutated'
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND plan_id = p_plan_id;
  EXCEPTION WHEN insufficient_privilege THEN
    update_denied := true;
  END;
  BEGIN
    DELETE FROM memory.projection_plan
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND plan_id = p_plan_id;
  EXCEPTION WHEN insufficient_privilege THEN
    delete_denied := true;
  END;
  IF NOT update_denied OR NOT delete_denied THEN
    RAISE EXCEPTION 'projection staging is not append-only';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_duplicate_packet_denied(p_plan_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.projection_plan(
      owner_user_id, plan_id, contract_version,
      predicate_registry_version, projection_policy_version,
      projector, projector_version, packet_text, packet_text_sha256,
      packet_sha256, owner_manifest_sha256, projection_count,
      invoked_by_session
    )
    SELECT owner_user_id, gen_random_uuid(), contract_version,
      predicate_registry_version, projection_policy_version,
      projector, projector_version, packet_text, packet_text_sha256,
      packet_sha256, owner_manifest_sha256, projection_count, session_user
    FROM memory.projection_plan
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND plan_id = p_plan_id;
  EXCEPTION WHEN unique_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'projection packet replay produced another plan';
  END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_incomplete_typed_payload_denied(p_plan_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  new_plan_id uuid := gen_random_uuid();
  packet_base jsonb;
  packet_value jsonb;
  packet_sha text;
  denied boolean := false;
BEGIN
  BEGIN
    SET CONSTRAINTS ALL DEFERRED;
    SELECT jsonb_set(
      (stored.packet_text::jsonb) - 'packet_sha256',
      '{projector_version}', to_jsonb('v5-incomplete'::text), false
    ) INTO STRICT packet_base
    FROM memory.projection_plan AS stored
    WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND stored.plan_id = p_plan_id;
    packet_sha := memory.v5_digest_text(
      memory.v5_canonical_json_text(packet_base)
    );
    packet_value := packet_base || jsonb_build_object(
      'packet_sha256', packet_sha
    );

    INSERT INTO memory.projection_plan(
      owner_user_id, plan_id, contract_version,
      predicate_registry_version, projection_policy_version,
      projector, projector_version, packet_text, packet_text_sha256,
      packet_sha256, owner_manifest_sha256, projection_count,
      invoked_by_session
    ) VALUES (
      '11111111-1111-4111-8111-111111111111', new_plan_id,
      'memory_v1_projection_plan_v5', 'memory_predicate_registry_v5',
      'memory_projection_policy_v5', 'projection_security_test',
      'v5-incomplete', packet_value::text,
      memory.v5_digest_text(packet_value::text), packet_sha,
      memory.v5_projection_owner_manifest_sha256(
        '11111111-1111-4111-8111-111111111111', packet_sha
      ), 3, session_user
    );

    INSERT INTO memory.projection_plan_item(
      owner_user_id, plan_id, projection_ref,
      predicate_registry_version, lane, projection, projection_sha256,
      subject_entity_id, predicate, object_kind, object_entity_id,
      object_literal_sha256, polarity, modality, lane_scope,
      semantic_key_sha256, target_action, expected_revision_number,
      target_reason_codes, temporal_materialization,
      temporal_source_observation_id, review_state,
      authorization_required, review_reason_codes
    )
    SELECT owner_user_id, new_plan_id, projection_ref,
      predicate_registry_version, lane, projection, projection_sha256,
      subject_entity_id, predicate, object_kind, object_entity_id,
      object_literal_sha256, polarity, modality, lane_scope,
      semantic_key_sha256, target_action, expected_revision_number,
      target_reason_codes, temporal_materialization,
      temporal_source_observation_id, review_state,
      authorization_required, review_reason_codes
    FROM memory.projection_plan_item
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND plan_id = p_plan_id;

    SET CONSTRAINTS ALL IMMEDIATE;
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'projection plan accepted items without typed payloads';
  END IF;
END
$function$;

-- Test-only and transactionally rolled back. NOINHERIT preserves the normal
-- brains_app boundary while permitting explicit fixture setup as the writer.
GRANT memory_v5_writer TO brains_app;
GRANT SELECT, INSERT ON memory.observation_entailment_v5
  TO memory_v5_writer;
SET SESSION AUTHORIZATION brains_app;
SET ROLE memory_v5_writer;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);

INSERT INTO memory.entity_mention(
  mention_id, owner_user_id, evidence_id, packet_sha256,
  entity_ref, entity_type, mention_kind, name_text, relationship_role,
  source_spans, extraction_confidence, reason_codes,
  extractor, extractor_version, mention_sha256
) VALUES (
  'a2222222-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111', repeat('9', 64),
  'e01', 'self', 'self_reference', NULL, NULL,
  jsonb_build_array(jsonb_build_object(
    'start', 0, 'end', 2, 'span_sha256', repeat('1', 64)
  )), 1.000, '[]'::jsonb,
  'projection_security_test', 'v5', repeat('2', 64)
), (
  'a2222222-1111-4111-8111-111111111112',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111', repeat('9', 64),
  'e02', 'project', 'named', 'Memory V1', NULL,
  jsonb_build_array(jsonb_build_object(
    'start', 41, 'end', 50, 'span_sha256', repeat('3', 64)
  )), 1.000, '[]'::jsonb,
  'projection_security_test', 'v5', repeat('4', 64)
);

INSERT INTO memory.entity_resolution_plan(
  resolution_id, owner_user_id, evidence_id, mention_id,
  predicate_registry_version, entity_normalization_version,
  resolver, resolver_version, action, decision_state,
  selected_entity_id, proposed_entity, candidate_set_sha256,
  decision_sha256, review_reason_codes
) VALUES (
  'a3333333-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111',
  'a2222222-1111-4111-8111-111111111111',
  'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
  'projection_security_test', 'v5', 'link_existing',
  'auto_link_eligible', 'a1111111-1111-4111-8111-111111111111',
  NULL, repeat('3', 64), repeat('4', 64), '[]'::jsonb
), (
  'a3333333-1111-4111-8111-111111111112',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111',
  'a2222222-1111-4111-8111-111111111112',
  'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
  'projection_security_test', 'v5', 'link_existing',
  'auto_link_eligible', 'a1111111-1111-4111-8111-111111111112',
  NULL, repeat('5', 64), repeat('6', 64), '[]'::jsonb
);

INSERT INTO memory.entity_resolution_candidate(
  owner_user_id, resolution_id, candidate_entity_id, ordinal,
  entity_type, features, exclusion_reasons
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a3333333-1111-4111-8111-111111111111',
  'a1111111-1111-4111-8111-111111111111', 1, 'self',
  jsonb_build_object(
    'active_status', true,
    'entity_type_match', true,
    'exact_canonical_name', false,
    'exact_alias', false,
    'relationship_role_supported', false,
    'source_local_coreference', true,
    'graph_neighbor_supported', true,
    'conflicting_attribute_count', 0,
    'same_name_candidate_count', 1
  ), '[]'::jsonb
), (
  '11111111-1111-4111-8111-111111111111',
  'a3333333-1111-4111-8111-111111111112',
  'a1111111-1111-4111-8111-111111111112', 1, 'project',
  jsonb_build_object(
    'active_status', true,
    'entity_type_match', true,
    'exact_canonical_name', true,
    'exact_alias', true,
    'relationship_role_supported', false,
    'source_local_coreference', true,
    'graph_neighbor_supported', true,
    'conflicting_attribute_count', 0,
    'same_name_candidate_count', 1
  ), '[]'::jsonb
);

INSERT INTO memory.entity_resolution_apply(
  owner_user_id, resolution_id, review_id, applied_entity_id,
  apply_manifest_sha256
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a3333333-1111-4111-8111-111111111111', NULL,
  'a1111111-1111-4111-8111-111111111111', repeat('5', 64)
), (
  '11111111-1111-4111-8111-111111111111',
  'a3333333-1111-4111-8111-111111111112', NULL,
  'a1111111-1111-4111-8111-111111111112', repeat('6', 64)
);

SELECT jsonb_build_object(
  'kind', 'literal', 'datatype', 'text', 'value', 'Eric',
  'unit', NULL, 'approximate', false
) AS claim_literal,
jsonb_build_object(
  'kind', 'literal', 'datatype', 'json',
  'value', jsonb_build_object('length', 'concise'),
  'unit', NULL, 'approximate', false
) AS preference_literal,
jsonb_build_object(
  'kind', 'literal', 'datatype', 'text',
  'value', 'Every account must remain isolated.',
  'unit', NULL, 'approximate', false
) AS project_literal
\gset

INSERT INTO memory.observation(
  observation_id, owner_user_id, evidence_id, observation_ref,
  subject_mention_id, predicate, predicate_registry_version,
  object_mention_id, object_literal, polarity, modality,
  projection_class, surface_policy, project_scope, sensitivity,
  extraction_confidence, source_spans, reason_codes, extractor,
  extractor_version, packet_sha256, observation_sha256
) VALUES
  (
    'a4444444-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111', 'o01',
    'a2222222-1111-4111-8111-111111111111',
    'identity.name', 'memory_predicate_registry_v5', NULL,
    :'claim_literal'::jsonb, 'affirmed', 'asserted',
    'direct_claim', 'direct_or_relevant',
    '{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}',
    'medium', 1.000,
    '[{"start":0,"end":16,"span_sha256":"1111111111111111111111111111111111111111111111111111111111111111"}]',
    '[]', 'projection_security_test', 'v5', repeat('9', 64), repeat('6', 64)
  ),
  (
    'a4444444-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111', 'o02',
    'a2222222-1111-4111-8111-111111111111',
    'preference.response', 'memory_predicate_registry_v5', NULL,
    :'preference_literal'::jsonb, 'affirmed', 'asserted',
    'response_preference', 'zero_token_control_only',
    '{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}',
    'low', 1.000,
    '[{"start":17,"end":40,"span_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}]',
    '[]', 'projection_security_test', 'v5', repeat('9', 64), repeat('7', 64)
  ),
  (
    'a4444444-1111-4111-8111-111111111113',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111', 'o03',
    'a2222222-1111-4111-8111-111111111112',
    'project.requirement', 'memory_predicate_registry_v5', NULL,
    :'project_literal'::jsonb, 'affirmed', 'asserted',
    'project_knowledge', 'exact_project_scope_only',
    '{"state":"resolved","project_key":"verbal-sage","component_key":"memory-v1","binding_source":"trusted_component_registry"}',
    'medium', 1.000,
    '[{"start":41,"end":76,"span_sha256":"3333333333333333333333333333333333333333333333333333333333333333"}]',
    '[]', 'projection_security_test', 'v5', repeat('9', 64), repeat('8', 64)
  );

INSERT INTO memory.observation_temporal(
  owner_user_id, observation_id, semantic, shape, basis, source_form,
  certainty, precision, instant_at, calendar_range, instant_range,
  relative_offset, recurrence, anchored_to_source_time,
  normalization_policy_version, reason_codes, normalized_sha256
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111111',
    'none', 'none', 'none', 'none', 'unknown', 'unknown',
    NULL, NULL, NULL, NULL, NULL, false,
    'memory_temporal_normalization_v5', '[]', repeat('1', 64)
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111112',
    'none', 'none', 'none', 'none', 'unknown', 'unknown',
    NULL, NULL, NULL, NULL, NULL, false,
    'memory_temporal_normalization_v5', '[]', repeat('2', 64)
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111113',
    'none', 'none', 'none', 'none', 'unknown', 'unknown',
    NULL, NULL, NULL, NULL, NULL, false,
    'memory_temporal_normalization_v5', '[]', repeat('3', 64)
  );

INSERT INTO memory.observation_entity_binding(
  owner_user_id, observation_id, subject_resolution_id,
  subject_entity_id, object_resolution_id, object_entity_id,
  binding_manifest_sha256
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111111',
    'a3333333-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111', NULL, NULL, repeat('a', 64)
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111112',
    'a3333333-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111', NULL, NULL, repeat('b', 64)
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111113',
    'a3333333-1111-4111-8111-111111111112',
    'a1111111-1111-4111-8111-111111111112', NULL, NULL, repeat('c', 64)
  );

INSERT INTO memory.observation_entailment_v5(
  decision_id, owner_user_id, observation_id, observation_sha256,
  evidence_id, evidence_content_sha256, policy_version, decision,
  reason_code, source_spans, authorization_manifest_sha256,
  assessor_type, assessor_ref, invoked_by_session
) VALUES
  (
    'd4444444-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111111', repeat('6', 64),
    'aeeeeeee-1111-4111-8111-111111111111', repeat('a', 64),
    'memory_v1_predicate_entailment_v5_1', 'accepted',
    'predicate_entailment_v5_1_accepted',
    '[{"start":0,"end":16,"span_sha256":"1111111111111111111111111111111111111111111111111111111111111111"}]',
    repeat('d', 64), 'system', 'projection_security_test', 'brains_app'
  ),
  (
    'd4444444-1111-4111-8111-111111111112',
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111112', repeat('7', 64),
    'aeeeeeee-1111-4111-8111-111111111111', repeat('a', 64),
    'memory_v1_predicate_entailment_v5_1', 'accepted',
    'predicate_entailment_v5_1_accepted',
    '[{"start":17,"end":40,"span_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}]',
    repeat('e', 64), 'system', 'projection_security_test', 'brains_app'
  ),
  (
    'd4444444-1111-4111-8111-111111111113',
    '11111111-1111-4111-8111-111111111111',
    'a4444444-1111-4111-8111-111111111113', repeat('8', 64),
    'aeeeeeee-1111-4111-8111-111111111111', repeat('a', 64),
    'memory_v1_predicate_entailment_v5_1', 'accepted',
    'predicate_entailment_v5_1_accepted',
    '[{"start":41,"end":76,"span_sha256":"3333333333333333333333333333333333333333333333333333333333333333"}]',
    repeat('f', 64), 'system', 'projection_security_test', 'brains_app'
  );

SELECT
  memory.v5_digest_text(memory.v5_canonical_json_text(:'claim_literal'::jsonb))
    AS claim_literal_sha,
  memory.v5_digest_text(memory.v5_canonical_json_text(:'preference_literal'::jsonb))
    AS preference_literal_sha,
  memory.v5_digest_text(memory.v5_canonical_json_text(:'project_literal'::jsonb))
    AS project_literal_sha
\gset

SELECT
  memory.v5_projection_semantic_key_sha256(
    '11111111-1111-4111-8111-111111111111', 'claim',
    'a1111111-1111-4111-8111-111111111111', 'identity.name',
    'literal', NULL, :'claim_literal_sha', 'affirmed', 'asserted', '{}'
  ) AS claim_semantic_sha,
  memory.v5_projection_semantic_key_sha256(
    '11111111-1111-4111-8111-111111111111', 'preference',
    'a1111111-1111-4111-8111-111111111111', 'preference.response',
    'literal', NULL, :'preference_literal_sha', 'affirmed', 'asserted',
    '{"preference_class":"response","domain":"response_style","preference_key":"response.length","scope":"user_global"}'
  ) AS preference_semantic_sha,
  memory.v5_projection_semantic_key_sha256(
    '11111111-1111-4111-8111-111111111111', 'project_knowledge',
    'a1111111-1111-4111-8111-111111111112', 'project.requirement',
    'literal', NULL, :'project_literal_sha', 'affirmed', 'asserted',
    '{"project_id":"a5555555-1111-4111-8111-111111111111","component_key":"memory-v1","binding_source":"trusted_component_registry","knowledge_kind":"requirement","knowledge_key":"memory.account_isolation"}'
  ) AS project_semantic_sha
\gset

SELECT jsonb_build_object(
  'projection_ref', 'p01', 'lane', 'claim',
  'observation_inputs', jsonb_build_array(jsonb_build_object(
    'observation_id', 'a4444444-1111-4111-8111-111111111111',
    'observation_sha256', repeat('6', 64), 'stance', 'supports'
  )),
  'identity', jsonb_build_object(
    'subject_entity_id', 'a1111111-1111-4111-8111-111111111111',
    'predicate', 'identity.name', 'object_kind', 'literal',
    'object_entity_id', NULL, 'object_literal_sha256', :'claim_literal_sha',
    'polarity', 'affirmed', 'modality', 'asserted',
    'semantic_key_sha256', :'claim_semantic_sha'
  ),
  'target', jsonb_build_object(
    'action', 'create', 'expected_revision_number', NULL,
    'aggregate_id', NULL, 'reason_codes', '[]'::jsonb
  ),
  'temporal_policy', jsonb_build_object(
    'canonical_source', 'memory.observation_temporal',
    'materialization', 'link_only', 'source_observation_id', NULL
  ),
  'review', jsonb_build_object(
    'state', 'auto_apply_eligible', 'authorization_required', false,
    'reason_codes', '[]'::jsonb
  ),
  'relations', '[]'::jsonb,
  'payload', jsonb_build_object(
    'kind', 'claim', 'claim_class', 'direct_claim',
    'canonical_text', 'The user reports that their name is Eric.',
    'surface_policy', 'direct_or_relevant'
  )
) AS claim_projection,
jsonb_build_object(
  'projection_ref', 'p02', 'lane', 'preference',
  'observation_inputs', jsonb_build_array(jsonb_build_object(
    'observation_id', 'a4444444-1111-4111-8111-111111111112',
    'observation_sha256', repeat('7', 64), 'stance', 'supports'
  )),
  'identity', jsonb_build_object(
    'subject_entity_id', 'a1111111-1111-4111-8111-111111111111',
    'predicate', 'preference.response', 'object_kind', 'literal',
    'object_entity_id', NULL, 'object_literal_sha256', :'preference_literal_sha',
    'polarity', 'affirmed', 'modality', 'asserted',
    'semantic_key_sha256', :'preference_semantic_sha'
  ),
  'target', jsonb_build_object(
    'action', 'create', 'expected_revision_number', NULL,
    'aggregate_id', NULL, 'reason_codes', '[]'::jsonb
  ),
  'temporal_policy', jsonb_build_object(
    'canonical_source', 'memory.observation_temporal',
    'materialization', 'link_only', 'source_observation_id', NULL
  ),
  'review', jsonb_build_object(
    'state', 'auto_apply_eligible', 'authorization_required', false,
    'reason_codes', '[]'::jsonb
  ),
  'relations', '[]'::jsonb,
  'payload', jsonb_build_object(
    'kind', 'preference', 'preference_class', 'response',
    'domain', 'response_style', 'preference_key', 'response.length',
    'value', jsonb_build_object('length', 'concise'),
    'preference_polarity', 'not_applicable', 'scope', 'user_global',
    'stability', 'stable', 'surface_policy', 'zero_token_control_only'
  )
) AS preference_projection,
jsonb_build_object(
  'projection_ref', 'p03', 'lane', 'project_knowledge',
  'observation_inputs', jsonb_build_array(jsonb_build_object(
    'observation_id', 'a4444444-1111-4111-8111-111111111113',
    'observation_sha256', repeat('8', 64), 'stance', 'supports'
  )),
  'identity', jsonb_build_object(
    'subject_entity_id', 'a1111111-1111-4111-8111-111111111112',
    'predicate', 'project.requirement', 'object_kind', 'literal',
    'object_entity_id', NULL, 'object_literal_sha256', :'project_literal_sha',
    'polarity', 'affirmed', 'modality', 'asserted',
    'semantic_key_sha256', :'project_semantic_sha'
  ),
  'target', jsonb_build_object(
    'action', 'create', 'expected_revision_number', NULL,
    'aggregate_id', NULL, 'reason_codes', '[]'::jsonb
  ),
  'temporal_policy', jsonb_build_object(
    'canonical_source', 'memory.observation_temporal',
    'materialization', 'link_only', 'source_observation_id', NULL
  ),
  'review', jsonb_build_object(
    'state', 'manual_review_required', 'authorization_required', true,
    'reason_codes', '["project_policy_requires_review"]'::jsonb
  ),
  'relations', '[]'::jsonb,
  'payload', jsonb_build_object(
    'kind', 'project_knowledge',
    'project_id', 'a5555555-1111-4111-8111-111111111111',
    'component_key', 'memory-v1',
    'binding_source', 'trusted_component_registry',
    'knowledge_kind', 'requirement',
    'knowledge_key', 'memory.account_isolation',
    'canonical_text', 'Every account must remain isolated.',
    'document_state', 'ratified', 'authority_level', 'user_ratified',
    'surface_policy', 'exact_project_scope_only'
  )
) AS project_projection
\gset

SELECT
  memory.v5_digest_text(memory.v5_canonical_json_text(:'claim_projection'::jsonb))
    AS claim_projection_sha,
  memory.v5_digest_text(memory.v5_canonical_json_text(:'preference_projection'::jsonb))
    AS preference_projection_sha,
  memory.v5_digest_text(memory.v5_canonical_json_text(:'project_projection'::jsonb))
    AS project_projection_sha
\gset

SELECT jsonb_build_object(
  'contract_version', 'memory_v1_projection_plan_v5',
  'predicate_registry_version', 'memory_predicate_registry_v5',
  'projection_policy_version', 'memory_projection_policy_v5',
  'projector', 'projection_security_test', 'projector_version', 'v5',
  'projections', jsonb_build_array(
    :'claim_projection'::jsonb,
    :'preference_projection'::jsonb,
    :'project_projection'::jsonb
  )
) AS packet_base
\gset

SELECT memory.v5_digest_text(
  memory.v5_canonical_json_text(:'packet_base'::jsonb)
) AS packet_sha
\gset

SELECT (:'packet_base'::jsonb || jsonb_build_object(
  'packet_sha256', :'packet_sha'
)) AS packet_value
\gset

INSERT INTO memory.projection_plan(
  owner_user_id, plan_id, contract_version, predicate_registry_version,
  projection_policy_version, projector, projector_version,
  packet_text, packet_text_sha256, packet_sha256,
  owner_manifest_sha256, projection_count, invoked_by_session
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111',
  'memory_v1_projection_plan_v5', 'memory_predicate_registry_v5',
  'memory_projection_policy_v5', 'projection_security_test', 'v5',
  :'packet_value', memory.v5_digest_text(:'packet_value'), :'packet_sha',
  memory.v5_projection_owner_manifest_sha256(
    '11111111-1111-4111-8111-111111111111', :'packet_sha'
  ), 3, session_user
);

INSERT INTO memory.projection_plan_item(
  owner_user_id, plan_id, projection_ref, predicate_registry_version,
  lane, projection, projection_sha256, subject_entity_id, predicate,
  object_kind, object_entity_id, object_literal_sha256, polarity,
  modality, lane_scope, semantic_key_sha256, target_action,
  expected_revision_number, target_reason_codes, temporal_materialization,
  temporal_source_observation_id, review_state, authorization_required,
  review_reason_codes
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p01',
    'memory_predicate_registry_v5', 'claim', :'claim_projection'::jsonb,
    :'claim_projection_sha', 'a1111111-1111-4111-8111-111111111111',
    'identity.name', 'literal', NULL, :'claim_literal_sha',
    'affirmed', 'asserted', '{}', :'claim_semantic_sha',
    'create', NULL, '[]', 'link_only', NULL,
    'auto_apply_eligible', false, '[]'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p02',
    'memory_predicate_registry_v5', 'preference', :'preference_projection'::jsonb,
    :'preference_projection_sha', 'a1111111-1111-4111-8111-111111111111',
    'preference.response', 'literal', NULL, :'preference_literal_sha',
    'affirmed', 'asserted',
    '{"preference_class":"response","domain":"response_style","preference_key":"response.length","scope":"user_global"}',
    :'preference_semantic_sha', 'create', NULL, '[]', 'link_only', NULL,
    'auto_apply_eligible', false, '[]'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p03',
    'memory_predicate_registry_v5', 'project_knowledge', :'project_projection'::jsonb,
    :'project_projection_sha', 'a1111111-1111-4111-8111-111111111112',
    'project.requirement', 'literal', NULL, :'project_literal_sha',
    'affirmed', 'asserted',
    '{"project_id":"a5555555-1111-4111-8111-111111111111","component_key":"memory-v1","binding_source":"trusted_component_registry","knowledge_kind":"requirement","knowledge_key":"memory.account_isolation"}',
    :'project_semantic_sha', 'create', NULL, '[]', 'link_only', NULL,
    'manual_review_required', true, '["project_policy_requires_review"]'
  );

INSERT INTO memory.projection_claim_payload(
  owner_user_id, plan_id, projection_ref, target_action,
  target_claim_id, claim_class, canonical_text, surface_policy
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 'p01', 'create', NULL,
  'direct_claim', 'The user reports that their name is Eric.',
  'direct_or_relevant'
);

INSERT INTO memory.projection_preference_payload(
  owner_user_id, plan_id, projection_ref, target_action,
  target_preference_id, preference_class, preference_domain,
  preference_key, value, preference_polarity, scope, stability,
  surface_policy
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 'p02', 'create', NULL,
  'response', 'response_style', 'response.length',
  '{"length":"concise"}', 'not_applicable', 'user_global', 'stable',
  'zero_token_control_only'
);

INSERT INTO memory.projection_project_payload(
  owner_user_id, plan_id, projection_ref, target_action,
  project_id, component_key, binding_source, target_knowledge_id,
  knowledge_kind, knowledge_key,
  canonical_text, document_state, authority_level, surface_policy
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 'p03', 'create',
  'a5555555-1111-4111-8111-111111111111', 'memory-v1',
  'trusted_component_registry', NULL,
  'requirement', 'memory.account_isolation',
  'Every account must remain isolated.', 'ratified', 'user_ratified',
  'exact_project_scope_only'
);

INSERT INTO memory.projection_plan_observation(
  owner_user_id, plan_id, projection_ref,
  observation_id, observation_sha256, stance
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p01',
    'a4444444-1111-4111-8111-111111111111', repeat('6', 64), 'supports'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p02',
    'a4444444-1111-4111-8111-111111111112', repeat('7', 64), 'supports'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a6666666-1111-4111-8111-111111111111', 'p03',
    'a4444444-1111-4111-8111-111111111113', repeat('8', 64), 'supports'
  );

SET CONSTRAINTS ALL IMMEDIATE;

DO $valid$
BEGIN
  IF (SELECT count(*) FROM memory.projection_plan) <> 1
     OR (SELECT count(*) FROM memory.projection_plan_item) <> 3
     OR (SELECT count(*) FROM memory.projection_claim_payload) <> 1
     OR (SELECT count(*) FROM memory.projection_preference_payload) <> 1
     OR (SELECT count(*) FROM memory.projection_project_payload) <> 1
     OR (SELECT count(*) FROM memory.projection_plan_observation) <> 3
     OR EXISTS (SELECT 1 FROM memory.projection_review)
     OR EXISTS (SELECT 1 FROM memory.projection_apply_event)
     OR EXISTS (SELECT 1 FROM memory.claim)
     OR EXISTS (SELECT 1 FROM memory.preference_head_v5)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_head_v5)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_revision_v5) THEN
    RAISE EXCEPTION 'valid projection plan produced unexpected rows';
  END IF;
END
$valid$;

SELECT pg_temp.assert_duplicate_packet_denied(
  'a6666666-1111-4111-8111-111111111111'
);
SELECT pg_temp.assert_append_only_denied(
  'a6666666-1111-4111-8111-111111111111'
);
SELECT pg_temp.assert_incomplete_typed_payload_denied(
  'a6666666-1111-4111-8111-111111111111'
);

SELECT set_config('app.user_id', '', true);
SELECT pg_temp.assert_actor_insert_denied(
  '11111111-1111-4111-8111-111111111111'
);

SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
SELECT pg_temp.assert_actor_insert_denied(
  '22222222-2222-4222-8222-222222222222'
);

SELECT set_config(
  'app.user_id', '22222222-2222-4222-8222-222222222222', true
);
DO $isolation$
BEGIN
  IF EXISTS (SELECT 1 FROM memory.projection_plan)
     OR EXISTS (SELECT 1 FROM memory.projection_plan_item)
     OR EXISTS (SELECT 1 FROM memory.projection_plan_observation) THEN
    RAISE EXCEPTION 'owner B can see owner A projection rows';
  END IF;
END
$isolation$;

RESET SESSION AUTHORIZATION;
SET SESSION AUTHORIZATION brains_app;
SELECT pg_temp.assert_application_read_denied();
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);

CREATE TEMP TABLE projection_test_manifest (
  manifest_name text PRIMARY KEY,
  manifest_sha256 text NOT NULL,
  review_id uuid
);
CREATE TEMP TABLE projection_test_review_result (
  result_name text PRIMARY KEY,
  review_id uuid NOT NULL,
  outcome text NOT NULL,
  rows_written integer NOT NULL,
  result jsonb NOT NULL
);
CREATE TEMP TABLE projection_test_apply_result (
  result_name text PRIMARY KEY,
  apply_event_id uuid NOT NULL,
  outcome text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL,
  aggregate_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  revision_number integer NOT NULL,
  rows_written integer NOT NULL,
  result jsonb NOT NULL
);

DO $manual_gate$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.preflight_projection_apply_v5(
      'a6666666-1111-4111-8111-111111111111', 'p03', NULL
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'manual projection apply was not review-gated';
  END IF;
END
$manual_gate$;

INSERT INTO projection_test_manifest(manifest_name, manifest_sha256)
SELECT 'project_review', authorization_manifest_sha256
FROM memory.preflight_projection_review_v5(
  'a6666666-1111-4111-8111-111111111111', 'p03', 'authorized',
  'user', '11111111-1111-4111-8111-111111111111',
  'Reviewed project requirement.', '["project_policy_requires_review"]'
);

INSERT INTO projection_test_review_result
SELECT 'project_review_apply', reviewed.*
FROM memory.review_projection_v5(
  'a6666666-1111-4111-8111-111111111111', 'p03', 'authorized',
  'user', '11111111-1111-4111-8111-111111111111',
  'Reviewed project requirement.', '["project_policy_requires_review"]',
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'project_review')
) AS reviewed;
UPDATE projection_test_manifest SET review_id = (
  SELECT review_id FROM projection_test_review_result
  WHERE result_name = 'project_review_apply'
) WHERE manifest_name = 'project_review';

INSERT INTO projection_test_review_result
SELECT 'project_review_replay', reviewed.*
FROM memory.review_projection_v5(
  'a6666666-1111-4111-8111-111111111111', 'p03', 'authorized',
  'user', '11111111-1111-4111-8111-111111111111',
  'Reviewed project requirement.', '["project_policy_requires_review"]',
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'project_review')
) AS reviewed;

INSERT INTO projection_test_manifest(manifest_name, manifest_sha256)
SELECT 'claim_apply', apply_manifest_sha256
FROM memory.preflight_projection_apply_v5(
  'a6666666-1111-4111-8111-111111111111', 'p01', NULL
);
INSERT INTO projection_test_manifest(manifest_name, manifest_sha256)
SELECT 'preference_apply', apply_manifest_sha256
FROM memory.preflight_projection_apply_v5(
  'a6666666-1111-4111-8111-111111111111', 'p02', NULL
);
INSERT INTO projection_test_manifest(manifest_name, manifest_sha256, review_id)
SELECT 'project_apply', apply_manifest_sha256,
  (SELECT review_id FROM projection_test_manifest
   WHERE manifest_name = 'project_review')
FROM memory.preflight_projection_apply_v5(
  'a6666666-1111-4111-8111-111111111111', 'p03',
  (SELECT review_id FROM projection_test_manifest
   WHERE manifest_name = 'project_review')
);

DO $wrong_manifest$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.apply_projection_v5(
      'a7777777-1111-4111-8111-111111111100',
      'a6666666-1111-4111-8111-111111111111', 'p02', NULL,
      repeat('f', 64)
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'projection apply accepted a wrong manifest';
  END IF;
END
$wrong_manifest$;

INSERT INTO projection_test_apply_result
SELECT 'claim_apply', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 'p01', NULL,
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'claim_apply')
) AS applied;
INSERT INTO projection_test_apply_result
SELECT 'preference_apply', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111112',
  'a6666666-1111-4111-8111-111111111111', 'p02', NULL,
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'preference_apply')
) AS applied;
INSERT INTO projection_test_apply_result
SELECT 'project_apply', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111113',
  'a6666666-1111-4111-8111-111111111111', 'p03',
  (SELECT review_id FROM projection_test_manifest
   WHERE manifest_name = 'project_apply'),
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'project_apply')
) AS applied;

DO $request_mismatch$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.apply_projection_v5(
      'a7777777-1111-4111-8111-111111111111',
      'a6666666-1111-4111-8111-111111111111', 'p02', NULL,
      (SELECT manifest_sha256 FROM projection_test_manifest
       WHERE manifest_name = 'preference_apply')
    );
  EXCEPTION WHEN check_violation THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'request_id replay accepted a different projection';
  END IF;
END
$request_mismatch$;

RESET SESSION AUTHORIZATION;

DO $applied_state$
BEGIN
  IF (SELECT count(*) FROM memory.projection_review WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.projection_apply_event WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 3
     OR (SELECT count(*) FROM memory.projection_dispatch_v5 WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 3
     OR (SELECT count(*) FROM memory.claim WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.claim_revision WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.claim_observation WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.preference_head_v5 WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.preference_revision_v5 WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.preference_revision_observation WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.project_knowledge_head_v5 WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM memory.project_knowledge_head_v5 AS head
       WHERE head.owner_user_id = '11111111-1111-4111-8111-111111111111'
         AND head.component_key = 'memory-v1'
         AND head.binding_source = 'trusted_component_registry'
     )
     OR (SELECT count(*) FROM memory.project_knowledge_revision_v5 WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT count(*) FROM memory.project_knowledge_revision_observation WHERE owner_user_id = '11111111-1111-4111-8111-111111111111') <> 1
     OR EXISTS (
       SELECT 1 FROM projection_test_review_result
       WHERE (result_name = 'project_review_apply'
              AND (outcome <> 'applied' OR rows_written <> 1))
          OR (result_name = 'project_review_replay'
              AND (outcome <> 'replayed' OR rows_written <> 0))
     )
     OR (SELECT count(*) FROM projection_test_apply_result
         WHERE outcome = 'applied' AND rows_written > 0) <> 3 THEN
    RAISE EXCEPTION 'controlled projection apply state is incorrect';
  END IF;
END
$applied_state$;

CREATE TEMP TABLE projection_replay_snapshot AS
SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
  jsonb_build_object(
    'claim', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY claim_id)
              FROM memory.claim AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'claim_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                       FROM memory.claim_revision AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'claim_observation', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY claim_id)
                          FROM memory.claim_observation AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'preference_head', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY preference_id)
                        FROM memory.preference_head_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'preference_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                            FROM memory.preference_revision_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'project_head', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY knowledge_id)
                     FROM memory.project_knowledge_head_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'project_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                         FROM memory.project_knowledge_revision_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'apply_event', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY event_id)
                    FROM memory.projection_apply_event AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
    'dispatch', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY dispatch_id)
                 FROM memory.projection_dispatch_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111')
  )
)) AS fingerprint;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
INSERT INTO projection_test_apply_result
SELECT 'claim_exact_replay', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111111',
  'a6666666-1111-4111-8111-111111111111', 'p01', NULL,
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'claim_apply')
) AS applied;
INSERT INTO projection_test_apply_result
SELECT 'preference_manifest_replay', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111122',
  'a6666666-1111-4111-8111-111111111111', 'p02', NULL,
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'preference_apply')
) AS applied;
INSERT INTO projection_test_apply_result
SELECT 'project_exact_replay', applied.*
FROM memory.apply_projection_v5(
  'a7777777-1111-4111-8111-111111111113',
  'a6666666-1111-4111-8111-111111111111', 'p03',
  (SELECT review_id FROM projection_test_manifest
   WHERE manifest_name = 'project_apply'),
  (SELECT manifest_sha256 FROM projection_test_manifest
   WHERE manifest_name = 'project_apply')
) AS applied;

SELECT set_config(
  'app.user_id', '22222222-2222-4222-8222-222222222222', true
);
DO $api_isolation$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    PERFORM 1 FROM memory.preflight_projection_apply_v5(
      'a6666666-1111-4111-8111-111111111111', 'p01', NULL
    );
  EXCEPTION WHEN no_data_found THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'owner B reached owner A projection API';
  END IF;
END
$api_isolation$;
RESET SESSION AUTHORIZATION;

DO $zero_write_replay$
DECLARE
  after_fingerprint text;
BEGIN
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'claim', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY claim_id)
                FROM memory.claim AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'claim_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                         FROM memory.claim_revision AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'claim_observation', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY claim_id)
                            FROM memory.claim_observation AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'preference_head', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY preference_id)
                          FROM memory.preference_head_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'preference_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                              FROM memory.preference_revision_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'project_head', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY knowledge_id)
                       FROM memory.project_knowledge_head_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'project_revision', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY revision_id)
                           FROM memory.project_knowledge_revision_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'apply_event', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY event_id)
                      FROM memory.projection_apply_event AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111'),
      'dispatch', (SELECT jsonb_agg(to_jsonb(stored) ORDER BY dispatch_id)
                   FROM memory.projection_dispatch_v5 AS stored
              WHERE stored.owner_user_id = '11111111-1111-4111-8111-111111111111')
    )
  )) INTO after_fingerprint;
  IF after_fingerprint <> (SELECT fingerprint FROM projection_replay_snapshot)
     OR (SELECT count(*) FROM projection_test_apply_result
         WHERE result_name LIKE '%replay'
           AND outcome = 'replayed' AND rows_written = 0) <> 3 THEN
    RAISE EXCEPTION 'projection replay was not zero-write';
  END IF;
END
$zero_write_replay$;

SET SESSION AUTHORIZATION brains_app;
SET ROLE memory_v5_writer;
SELECT set_config(
  'app.user_id', '11111111-1111-4111-8111-111111111111', true
);
INSERT INTO memory.project_knowledge_head_v5(
  owner_user_id, project_id, component_key, binding_source,
  semantic_key_sha256, knowledge_kind, knowledge_key
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a5555555-1111-4111-8111-111111111111', NULL,
    'trusted_thread_binding', repeat('d', 64),
    'requirement', 'memory.account_isolation'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a5555555-1111-4111-8111-111111111111', 'resse',
    'trusted_component_registry', repeat('e', 64),
    'requirement', 'memory.account_isolation'
  );
DO $scope_isolation$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.project_knowledge_head_v5(
      owner_user_id, project_id, component_key, binding_source,
      semantic_key_sha256, knowledge_kind, knowledge_key
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'a5555555-1111-4111-8111-111111111111', 'owner-b-only',
      'trusted_component_registry', repeat('f', 64),
      'requirement', 'memory.cross_owner_rejected'
    );
  EXCEPTION WHEN foreign_key_violation THEN
    denied := true;
  END;
  IF NOT denied OR (
    SELECT count(*) FROM memory.project_knowledge_head_v5
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
      AND knowledge_key = 'memory.account_isolation'
  ) <> 3 THEN
    RAISE EXCEPTION 'root, sibling, or cross-owner component scope failed';
  END IF;
END
$scope_isolation$;
RESET SESSION AUTHORIZATION;

ROLLBACK;

DO $empty$
BEGIN
  IF EXISTS (SELECT 1 FROM memory.projection_plan)
     OR EXISTS (SELECT 1 FROM memory.projection_plan_item)
     OR EXISTS (SELECT 1 FROM memory.projection_claim_payload)
     OR EXISTS (SELECT 1 FROM memory.projection_preference_payload)
     OR EXISTS (SELECT 1 FROM memory.projection_project_payload)
     OR EXISTS (SELECT 1 FROM memory.projection_plan_observation)
     OR EXISTS (SELECT 1 FROM memory.projection_plan_relation)
     OR EXISTS (SELECT 1 FROM memory.projection_review)
     OR EXISTS (SELECT 1 FROM memory.projection_apply_event)
     OR EXISTS (SELECT 1 FROM memory.preference_revision_observation)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_revision_observation)
     OR EXISTS (SELECT 1 FROM memory.claim_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.preference_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.projection_dispatch_v5) THEN
    RAISE EXCEPTION 'projection security suite left rows behind';
  END IF;
END
$empty$;

SELECT 'memory_v1_projection_apply_production_v5: PASS' AS result;
