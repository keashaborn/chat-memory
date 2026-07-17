\set ON_ERROR_STOP on

DO $block$
DECLARE
  expected_tables text[] := ARRAY[
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
  ];
  table_name text;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'clone security suite must start as sage';
  END IF;
  FOREACH table_name IN ARRAY expected_tables LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = table_name
        AND relation.relkind = 'r'
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'missing forced RLS on memory.%', table_name;
    END IF;
    IF has_table_privilege('brains_app', format('memory.%I', table_name), 'SELECT')
       OR has_table_privilege('brains_app', format('memory.%I', table_name), 'INSERT')
       OR has_table_privilege('brains_app', format('memory.%I', table_name), 'UPDATE')
       OR has_table_privilege('brains_app', format('memory.%I', table_name), 'DELETE') THEN
      RAISE EXCEPTION 'brains_app unexpectedly has access to memory.%', table_name;
    END IF;
  END LOOP;
  IF NOT has_table_privilege(
    'brains_app', 'memory.predicate_contract', 'SELECT'
  ) THEN
    RAISE EXCEPTION 'brains_app must have read-only predicate contract access';
  END IF;
  IF has_table_privilege('brains_app', 'memory.predicate_contract', 'INSERT')
     OR has_table_privilege('brains_app', 'memory.predicate_registry_seed', 'SELECT') THEN
    RAISE EXCEPTION 'predicate registry grants are broader than intended';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'memory'
      AND (
        procedure.proname LIKE 'v5\_%' ESCAPE '\'
        OR procedure.proname LIKE 'guard\_v5\_%' ESCAPE '\'
      )
      AND procedure.prosecdef
  ) THEN
    RAISE EXCEPTION 'V5 functions must all be SECURITY INVOKER';
  END IF;
  IF (
    SELECT count(*) FROM memory.predicate_contract
    WHERE registry_version = 'memory_predicate_registry_v5'
      AND lifecycle = 'active' AND extraction_allowed
  ) <> 25 THEN
    RAISE EXCEPTION 'expected 25 active V5 predicates';
  END IF;
  IF (
    SELECT count(*) FROM memory.predicate_contract
    WHERE registry_version = 'memory_predicate_registry_v5'
      AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed
  ) <> 19 THEN
    RAISE EXCEPTION 'expected 19 legacy read-only predicates';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = 'memory_predicate_registry_v5'
      AND (runtime_active OR status <> 'proposed')
  ) THEN
    RAISE EXCEPTION 'V5 registry must remain proposed and runtime-inactive';
  END IF;
END
$block$;

BEGIN;

CREATE ROLE memory_v5_clone_writer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

GRANT USAGE ON SCHEMA memory TO memory_v5_clone_writer;
GRANT SELECT ON
  memory.predicate,
  memory.predicate_registry_version,
  memory.predicate_contract,
  memory.entity,
  memory.evidence,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.claim_observation,
  memory.candidate_observation
TO memory_v5_clone_writer;

GRANT INSERT ON memory.entity, memory.evidence TO memory_v5_clone_writer;
GRANT INSERT, UPDATE, DELETE ON
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.claim_observation,
  memory.candidate_observation
TO memory_v5_clone_writer;

GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb, integer)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_source_spans_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_project_scope_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_literal_object_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_proposed_entity_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_candidate_features_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_relative_offset_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.v5_recurrence_valid(jsonb)
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_actor_active_evidence()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_plan()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_observation_contract()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_review()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_apply()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_observation_binding()
  TO memory_v5_clone_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only()
  TO memory_v5_clone_writer;

SET LOCAL ROLE memory_v5_clone_writer;
SET LOCAL app.user_id = '11111111-1111-4111-8111-111111111111';

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
    'animal:koda:2', 'animal', 'Koda', 'koda'
  );

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, observed_at, sensitivity
) VALUES (
  'aeeeeeee-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'user_statement', 'clone_test', 'owner-a-source',
  'My dog Koda lives with me.', repeat('a', 64),
  '2026-07-15T12:00:00Z', 'medium'
);

INSERT INTO memory.entity_mention(
  mention_id, owner_user_id, evidence_id, packet_sha256,
  entity_ref, entity_type, mention_kind, name_text, relationship_role,
  source_spans, extraction_confidence, reason_codes,
  extractor, extractor_version, mention_sha256
) VALUES
  (
    'a0000001-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111', repeat('1', 64),
    'e01', 'self', 'self_reference', NULL, NULL,
    jsonb_build_array(jsonb_build_object(
      'start', 0, 'end', 2, 'span_sha256', repeat('2', 64)
    )),
    1.000, '[]', 'clone_test', 'v5', repeat('3', 64)
  ),
  (
    'a0000002-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111', repeat('1', 64),
    'e02', 'animal', 'named', 'Koda', 'pet',
    jsonb_build_array(jsonb_build_object(
      'start', 7, 'end', 11, 'span_sha256', repeat('4', 64)
    )),
    0.990, '[]', 'clone_test', 'v5', repeat('5', 64)
  );

INSERT INTO memory.entity_resolution_plan(
  resolution_id, owner_user_id, evidence_id, mention_id,
  predicate_registry_version, entity_normalization_version,
  resolver, resolver_version, action, decision_state,
  selected_entity_id, proposed_entity, candidate_set_sha256,
  decision_sha256, review_reason_codes
) VALUES
  (
    'a1000001-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111',
    'a0000001-1111-4111-8111-111111111111',
    'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
    'trusted_owner_resolver', 'v5', 'link_existing', 'auto_link_eligible',
    'a1111111-1111-4111-8111-111111111111', NULL,
    repeat('6', 64), repeat('7', 64), '[]'
  ),
  (
    'a1000002-1111-4111-8111-111111111111',
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111',
    'a0000002-1111-4111-8111-111111111111',
    'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
    'trusted_owner_resolver', 'v5', 'link_existing', 'auto_link_eligible',
    'a1111111-1111-4111-8111-111111111112', NULL,
    repeat('8', 64), repeat('9', 64), '[]'
  );

INSERT INTO memory.entity_resolution_candidate(
  owner_user_id, resolution_id, candidate_entity_id,
  ordinal, entity_type, features, exclusion_reasons
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a1000001-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111111', 1, 'self',
    '{"active_status":true,"entity_type_match":true,"exact_canonical_name":false,"exact_alias":false,"relationship_role_supported":false,"source_local_coreference":true,"graph_neighbor_supported":true,"conflicting_attribute_count":0,"same_name_candidate_count":1}',
    '[]'
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a1000002-1111-4111-8111-111111111111',
    'a1111111-1111-4111-8111-111111111112', 1, 'animal',
    '{"active_status":true,"entity_type_match":true,"exact_canonical_name":true,"exact_alias":false,"relationship_role_supported":true,"source_local_coreference":false,"graph_neighbor_supported":false,"conflicting_attribute_count":0,"same_name_candidate_count":1}',
    '[]'
  );

INSERT INTO memory.entity_resolution_apply(
  owner_user_id, resolution_id, review_id, applied_entity_id,
  apply_manifest_sha256
) VALUES
  (
    '11111111-1111-4111-8111-111111111111',
    'a1000001-1111-4111-8111-111111111111', NULL,
    'a1111111-1111-4111-8111-111111111111', repeat('a', 64)
  ),
  (
    '11111111-1111-4111-8111-111111111111',
    'a1000002-1111-4111-8111-111111111111', NULL,
    'a1111111-1111-4111-8111-111111111112', repeat('b', 64)
  );

INSERT INTO memory.entity_alias_observation(
  alias_observation_id, owner_user_id, evidence_id, mention_id,
  resolution_id, entity_id, alias_text, normalized_alias, alias_type,
  source_spans, normalization_version, alias_sha256
) VALUES (
  'a2000001-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111',
  'a0000002-1111-4111-8111-111111111111',
  'a1000002-1111-4111-8111-111111111111',
  'a1111111-1111-4111-8111-111111111112',
  'Koda', 'koda', 'observed_name',
  jsonb_build_array(jsonb_build_object(
    'start', 7, 'end', 11, 'span_sha256', repeat('4', 64)
  )),
  'memory_entity_normalization_v5', repeat('c', 64)
);

INSERT INTO memory.observation(
  observation_id, owner_user_id, evidence_id, observation_ref,
  subject_mention_id, predicate, predicate_registry_version,
  object_mention_id, object_literal, polarity, modality,
  projection_class, surface_policy, project_scope, sensitivity,
  extraction_confidence, source_spans, reason_codes,
  extractor, extractor_version, packet_sha256, observation_sha256
) VALUES (
  'a3000001-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111', 'o01',
  'a0000001-1111-4111-8111-111111111111',
  'relationship.has_pet', 'memory_predicate_registry_v5',
  'a0000002-1111-4111-8111-111111111111', NULL,
  'affirmed', 'asserted', 'direct_claim', 'direct_or_relevant',
  '{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}',
  'medium', 0.990,
  jsonb_build_array(jsonb_build_object(
    'start', 0, 'end', 11, 'span_sha256', repeat('d', 64)
  )),
  '[]', 'clone_test', 'v5', repeat('1', 64), repeat('e', 64)
);

INSERT INTO memory.observation_temporal(
  owner_user_id, observation_id, semantic, shape, basis, source_form,
  certainty, precision, instant_at, calendar_range, instant_range,
  relative_offset, recurrence, anchored_to_source_time,
  normalization_policy_version, reason_codes, normalized_sha256
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a3000001-1111-4111-8111-111111111111',
  'none', 'none', 'none', 'none', 'unknown', 'unknown',
  NULL, NULL, NULL, NULL, NULL, false,
  'memory_temporal_normalization_v5', '[]', repeat('f', 64)
);

INSERT INTO memory.observation_entity_binding(
  owner_user_id, observation_id,
  subject_resolution_id, subject_entity_id,
  object_resolution_id, object_entity_id, binding_manifest_sha256
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'a3000001-1111-4111-8111-111111111111',
  'a1000001-1111-4111-8111-111111111111',
  'a1111111-1111-4111-8111-111111111111',
  'a1000002-1111-4111-8111-111111111111',
  'a1111111-1111-4111-8111-111111111112', repeat('0', 64)
);

DO $block$
BEGIN
  IF (SELECT count(*) FROM memory.entity_mention) <> 2
     OR (SELECT count(*) FROM memory.entity_resolution_apply) <> 2
     OR (SELECT count(*) FROM memory.observation) <> 1
     OR (SELECT count(*) FROM memory.observation_temporal) <> 1
     OR (SELECT count(*) FROM memory.observation_entity_binding) <> 1 THEN
    RAISE EXCEPTION 'owner A happy-path staging counts are wrong';
  END IF;
END
$block$;

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    UPDATE memory.observation
    SET extraction_confidence = extraction_confidence
    WHERE observation_id = 'a3000001-1111-4111-8111-111111111111';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'append-only update was not blocked';
  END IF;
END
$block$;

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    DELETE FROM memory.entity_mention
    WHERE mention_id = 'a0000001-1111-4111-8111-111111111111';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'append-only delete was not blocked';
  END IF;
END
$block$;

SET LOCAL app.user_id = '22222222-2222-4222-8222-222222222222';

INSERT INTO memory.entity(
  entity_id, owner_user_id, entity_key, entity_type,
  canonical_name, normalized_name
) VALUES (
  'b2222222-2222-4222-8222-222222222222',
  '22222222-2222-4222-8222-222222222222',
  'animal:koda:1', 'animal', 'Koda', 'koda'
);

INSERT INTO memory.evidence(
  evidence_id, owner_user_id, kind, source_system, external_id,
  content, content_sha256, observed_at, sensitivity
) VALUES (
  'beeeeeee-2222-4222-8222-222222222222',
  '22222222-2222-4222-8222-222222222222',
  'user_statement', 'clone_test', 'owner-b-source',
  'My dog Koda is different.', repeat('b', 64),
  '2026-07-15T12:01:00Z', 'medium'
);

INSERT INTO memory.entity_mention(
  mention_id, owner_user_id, evidence_id, packet_sha256,
  entity_ref, entity_type, mention_kind, name_text, relationship_role,
  source_spans, extraction_confidence, reason_codes,
  extractor, extractor_version, mention_sha256
) VALUES (
  'b0000001-2222-4222-8222-222222222222',
  '22222222-2222-4222-8222-222222222222',
  'beeeeeee-2222-4222-8222-222222222222', repeat('b', 64),
  'e01', 'animal', 'named', 'Koda', 'pet',
  jsonb_build_array(jsonb_build_object(
    'start', 7, 'end', 11, 'span_sha256', repeat('1', 64)
  )),
  0.990, '[]', 'clone_test', 'v5', repeat('2', 64)
);

DO $block$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.entity_mention
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
  ) OR EXISTS (
    SELECT 1 FROM memory.observation
    WHERE owner_user_id = '11111111-1111-4111-8111-111111111111'
  ) THEN
    RAISE EXCEPTION 'owner B can see owner A V5 rows';
  END IF;
  IF (SELECT count(*) FROM memory.entity WHERE normalized_name = 'koda') <> 1 THEN
    RAISE EXCEPTION 'same names must remain owner-scoped';
  END IF;
END
$block$;

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.entity_resolution_plan(
      resolution_id, owner_user_id, evidence_id, mention_id,
      predicate_registry_version, entity_normalization_version,
      resolver, resolver_version, action, decision_state,
      selected_entity_id, candidate_set_sha256, decision_sha256
    ) VALUES (
      'b1000001-2222-4222-8222-222222222222',
      '22222222-2222-4222-8222-222222222222',
      'beeeeeee-2222-4222-8222-222222222222',
      'b0000001-2222-4222-8222-222222222222',
      'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
      'trusted_owner_resolver', 'v5', 'link_existing', 'auto_link_eligible',
      'a1111111-1111-4111-8111-111111111112', repeat('3', 64), repeat('4', 64)
    );
  EXCEPTION WHEN foreign_key_violation OR insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'cross-owner entity resolution target was not blocked';
  END IF;
END
$block$;

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.entity_mention(
      owner_user_id, evidence_id, packet_sha256,
      entity_ref, entity_type, mention_kind, name_text,
      source_spans, extraction_confidence, extractor,
      extractor_version, mention_sha256
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'aeeeeeee-1111-4111-8111-111111111111', repeat('5', 64),
      'e09', 'animal', 'named', 'Stolen',
      jsonb_build_array(jsonb_build_object(
        'start', 0, 'end', 6, 'span_sha256', repeat('6', 64)
      )), 0.9, 'clone_test', 'v5', repeat('7', 64)
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'actor B wrote an owner A mention';
  END IF;
END
$block$;

SET LOCAL app.user_id = '';

DO $block$
DECLARE blocked boolean := false;
BEGIN
  IF (SELECT count(*) FROM memory.entity_mention) <> 0
     OR (SELECT count(*) FROM memory.observation) <> 0 THEN
    RAISE EXCEPTION 'missing actor can read V5 owner rows';
  END IF;
  BEGIN
    INSERT INTO memory.entity_mention(
      owner_user_id, evidence_id, packet_sha256,
      entity_ref, entity_type, mention_kind, name_text,
      source_spans, extraction_confidence, extractor,
      extractor_version, mention_sha256
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'aeeeeeee-1111-4111-8111-111111111111', repeat('8', 64),
      'e08', 'animal', 'named', 'No Actor',
      jsonb_build_array(jsonb_build_object(
        'start', 0, 'end', 8, 'span_sha256', repeat('9', 64)
      )), 0.9, 'clone_test', 'v5', repeat('0', 64)
    );
  EXCEPTION WHEN insufficient_privilege THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'missing actor write was not blocked';
  END IF;
END
$block$;

SET LOCAL app.user_id = '11111111-1111-4111-8111-111111111111';

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.observation(
      observation_id, owner_user_id, evidence_id, observation_ref,
      subject_mention_id, predicate, predicate_registry_version,
      object_literal, polarity, modality, projection_class, surface_policy,
      project_scope, sensitivity, extraction_confidence, source_spans,
      extractor, extractor_version, packet_sha256, observation_sha256
    ) VALUES (
      'a3000002-1111-4111-8111-111111111111',
      '11111111-1111-4111-8111-111111111111',
      'aeeeeeee-1111-4111-8111-111111111111', 'o02',
      'a0000001-1111-4111-8111-111111111111',
      'does_activity', 'memory_predicate_registry_v5',
      '{"kind":"literal","datatype":"text","value":"walking","unit":null,"approximate":false}',
      'affirmed', 'asserted', 'direct_claim', 'direct_or_relevant',
      '{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}',
      'medium', 0.9,
      jsonb_build_array(jsonb_build_object(
        'start', 0, 'end', 2, 'span_sha256', repeat('1', 64)
      )), 'clone_test', 'v5', repeat('2', 64), repeat('3', 64)
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'legacy predicate was accepted for V5 extraction';
  END IF;
END
$block$;

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.observation(
      observation_id, owner_user_id, evidence_id, observation_ref,
      subject_mention_id, predicate, predicate_registry_version,
      object_literal, polarity, modality, projection_class, surface_policy,
      project_scope, sensitivity, extraction_confidence, source_spans,
      extractor, extractor_version, packet_sha256, observation_sha256
    ) VALUES (
      'a3000003-1111-4111-8111-111111111111',
      '11111111-1111-4111-8111-111111111111',
      'aeeeeeee-1111-4111-8111-111111111111', 'o03',
      'a0000001-1111-4111-8111-111111111111',
      'project.requirement', 'memory_predicate_registry_v5',
      '{"kind":"literal","datatype":"text","value":"unsafe scope","unit":null,"approximate":false}',
      'affirmed', 'asserted', 'project_knowledge', 'exact_project_scope_only',
      '{"state":"unresolved","project_key":null,"binding_source":"unresolved"}',
      'medium', 0.9,
      jsonb_build_array(jsonb_build_object(
        'start', 0, 'end', 2, 'span_sha256', repeat('4', 64)
      )), 'clone_test', 'v5', repeat('5', 64), repeat('6', 64)
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'unresolved project scope was accepted';
  END IF;
END
$block$;

INSERT INTO memory.observation(
  observation_id, owner_user_id, evidence_id, observation_ref,
  subject_mention_id, predicate, predicate_registry_version,
  object_literal, polarity, modality, projection_class, surface_policy,
  project_scope, sensitivity, extraction_confidence, source_spans,
  extractor, extractor_version, packet_sha256, observation_sha256
) VALUES (
  'a3000004-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111', 'o04',
  'a0000002-1111-4111-8111-111111111111',
  'age.reported', 'memory_predicate_registry_v5',
  '{"kind":"literal","datatype":"number","value":7,"unit":"year","approximate":false}',
  'affirmed', 'reported_observation', 'direct_claim', 'direct_or_relevant',
  '{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}',
  'medium', 0.9,
  jsonb_build_array(jsonb_build_object(
    'start', 0, 'end', 2, 'span_sha256', repeat('7', 64)
  )), 'clone_test', 'v5', repeat('8', 64), repeat('9', 64)
);

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.observation_temporal(
      owner_user_id, observation_id, semantic, shape, basis, source_form,
      certainty, precision, instant_at, anchored_to_source_time,
      normalization_policy_version, normalized_sha256
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',
      'a3000004-1111-4111-8111-111111111111',
      'observation_time', 'instant', 'instant', 'absolute',
      'exact', 'month', '2026-07-15T12:00:00Z', false,
      'memory_temporal_normalization_v5', repeat('a', 64)
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'month precision was accepted as an exact instant';
  END IF;
END
$block$;

INSERT INTO memory.entity_mention(
  mention_id, owner_user_id, evidence_id, packet_sha256,
  entity_ref, entity_type, mention_kind, name_text,
  source_spans, extraction_confidence, reason_codes,
  extractor, extractor_version, mention_sha256
) VALUES (
  'a0000003-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111', repeat('1', 64),
  'e03', 'project', 'named', 'Verbal Sage',
  jsonb_build_array(jsonb_build_object(
    'start', 0, 'end', 2, 'span_sha256', repeat('b', 64)
  )),
  0.9, '[]', 'clone_test', 'v5', repeat('c', 64)
);

DO $block$
DECLARE blocked boolean := false;
BEGIN
  BEGIN
    INSERT INTO memory.entity_resolution_plan(
      resolution_id, owner_user_id, evidence_id, mention_id,
      predicate_registry_version, entity_normalization_version,
      resolver, resolver_version, action, decision_state,
      proposed_entity, candidate_set_sha256, decision_sha256,
      review_reason_codes
    ) VALUES (
      'a1000003-1111-4111-8111-111111111111',
      '11111111-1111-4111-8111-111111111111',
      'aeeeeeee-1111-4111-8111-111111111111',
      'a0000003-1111-4111-8111-111111111111',
      'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
      'model_resolver', 'v5', 'create_new', 'manual_review_required',
      '{"entity_type":"concept","identity_state":"named","canonical_name":"Verbal Sage","display_label":"Verbal Sage","creation_reason":"model_proposal"}',
      repeat('d', 64), repeat('e', 64), '["trusted_project_scope_required"]'
    );
  EXCEPTION WHEN check_violation THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'model-created project entity was not blocked';
  END IF;
END
$block$;

ROLLBACK;

DO $block$
DECLARE
  table_name text;
  row_count bigint;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
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
  ] LOOP
    EXECUTE format('SELECT count(*) FROM memory.%I', table_name) INTO row_count;
    IF row_count <> 0 THEN
      RAISE EXCEPTION 'clone test left % rows in memory.%', row_count, table_name;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_v5_clone_writer') THEN
    RAISE EXCEPTION 'clone test role survived rollback';
  END IF;
END
$block$;

SELECT 'memory_v1_relational_staging_v5: PASS' AS result;
