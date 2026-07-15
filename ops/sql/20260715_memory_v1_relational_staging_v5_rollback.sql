BEGIN;

DO $block$
DECLARE
  table_name text;
  row_count bigint;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 relational staging V5 rollback must run as sage, current_user=%',
      current_user;
  END IF;
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
  ]
  LOOP
    IF to_regclass('memory.' || table_name) IS NOT NULL THEN
      EXECUTE format('SELECT count(*) FROM memory.%I', table_name)
        INTO row_count;
      IF row_count <> 0 THEN
        RAISE EXCEPTION
          'refusing V5 rollback: memory.% contains % rows', table_name, row_count;
      END IF;
    END IF;
  END LOOP;
END
$block$;

DROP TABLE IF EXISTS memory.candidate_observation;
DROP TABLE IF EXISTS memory.claim_observation;
DROP TABLE IF EXISTS memory.observation_entity_binding;
DROP TABLE IF EXISTS memory.observation_temporal;
DROP TABLE IF EXISTS memory.observation;
DROP TABLE IF EXISTS memory.entity_alias_observation;
DROP TABLE IF EXISTS memory.entity_resolution_apply;
DROP TABLE IF EXISTS memory.entity_resolution_review;
DROP TABLE IF EXISTS memory.entity_resolution_candidate;
DROP TABLE IF EXISTS memory.entity_resolution_plan;
DROP TABLE IF EXISTS memory.entity_mention;

DROP FUNCTION IF EXISTS memory.guard_v5_observation_binding();
DROP FUNCTION IF EXISTS memory.guard_v5_resolution_apply();
DROP FUNCTION IF EXISTS memory.guard_v5_resolution_review();
DROP FUNCTION IF EXISTS memory.guard_v5_observation_contract();
DROP FUNCTION IF EXISTS memory.guard_v5_resolution_plan();
DROP FUNCTION IF EXISTS memory.guard_v5_actor_active_evidence();
DROP FUNCTION IF EXISTS memory.guard_v5_append_only();

DROP FUNCTION IF EXISTS memory.v5_recurrence_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_relative_offset_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_candidate_features_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_proposed_entity_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_literal_object_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_project_scope_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_source_spans_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_reason_codes_valid(jsonb, integer);

DROP TABLE IF EXISTS memory.predicate_contract;

CREATE TEMP TABLE v5_predicates_to_remove ON COMMIT DROP AS
SELECT predicate
FROM memory.predicate_registry_seed
WHERE registry_version = 'memory_predicate_registry_v5'
  AND base_predicate_created;

DROP TABLE IF EXISTS memory.predicate_registry_seed;

DELETE FROM memory.predicate AS predicate
USING v5_predicates_to_remove AS removal
WHERE predicate.predicate = removal.predicate;

DROP TABLE IF EXISTS memory.predicate_registry_version;

DROP FUNCTION IF EXISTS memory.v5_sha256_valid(text);

DROP TYPE IF EXISTS memory.temporal_precision;
DROP TYPE IF EXISTS memory.temporal_certainty;
DROP TYPE IF EXISTS memory.temporal_source_form;
DROP TYPE IF EXISTS memory.temporal_basis;
DROP TYPE IF EXISTS memory.temporal_shape;
DROP TYPE IF EXISTS memory.temporal_semantic;
DROP TYPE IF EXISTS memory.observation_surface_policy;
DROP TYPE IF EXISTS memory.observation_projection_class;
DROP TYPE IF EXISTS memory.observation_modality;
DROP TYPE IF EXISTS memory.observation_polarity;
DROP TYPE IF EXISTS memory.entity_review_decision;
DROP TYPE IF EXISTS memory.entity_resolution_state;
DROP TYPE IF EXISTS memory.entity_resolution_action;
DROP TYPE IF EXISTS memory.entity_mention_kind;

COMMIT;
