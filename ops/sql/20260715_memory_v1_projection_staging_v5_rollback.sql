BEGIN;

DO $block$
DECLARE
  relation_name text;
  has_rows boolean;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection staging V5 rollback must run as sage, current_user=%',
      current_user;
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
    'project_knowledge_revision_observation'
  ]
  LOOP
    IF to_regclass(format('memory.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM memory.%I)',
        relation_name
      ) INTO STRICT has_rows;
      IF has_rows THEN
        RAISE EXCEPTION
          'refusing projection staging rollback: memory.% contains rows',
          relation_name;
      END IF;
    END IF;
  END LOOP;
END
$block$;

DROP TABLE IF EXISTS memory.project_knowledge_revision_observation;
DROP TABLE IF EXISTS memory.preference_revision_observation;
DROP TABLE IF EXISTS memory.projection_apply_event;
DROP TABLE IF EXISTS memory.projection_review;
DROP TABLE IF EXISTS memory.projection_plan_relation;
ALTER TABLE IF EXISTS memory.projection_plan_item
  DROP CONSTRAINT IF EXISTS projection_item_temporal_input_fk;
DROP TABLE IF EXISTS memory.projection_plan_observation;
DROP TABLE IF EXISTS memory.projection_project_payload;
DROP TABLE IF EXISTS memory.projection_preference_payload;
DROP TABLE IF EXISTS memory.projection_claim_payload;
DROP TABLE IF EXISTS memory.projection_plan_item;
DROP TABLE IF EXISTS memory.projection_plan;

DO $block$
BEGIN
  IF to_regclass('memory.observation') IS NOT NULL THEN
    ALTER TABLE memory.observation
      DROP CONSTRAINT IF EXISTS observation_owner_id_sha_uq;
  END IF;
END
$block$;

DROP FUNCTION IF EXISTS memory.guard_projection_item_complete_v5();
DROP FUNCTION IF EXISTS memory.guard_projection_plan_complete_v5();
DROP FUNCTION IF EXISTS memory.guard_projection_actor_v5();
DROP FUNCTION IF EXISTS memory.v5_projection_semantic_key_sha256(
  uuid, memory.projection_lane_v5, uuid, text, text, uuid, text,
  memory.observation_polarity, memory.observation_modality, jsonb
);
DROP FUNCTION IF EXISTS memory.v5_projection_owner_manifest_sha256(uuid, text);
DROP FUNCTION IF EXISTS memory.v5_canonical_json_text(jsonb);

DROP TYPE IF EXISTS memory.projection_review_decision_v5;
DROP TYPE IF EXISTS memory.projection_relation_type_v5;
DROP TYPE IF EXISTS memory.projection_temporal_materialization_v5;
DROP TYPE IF EXISTS memory.projection_observation_stance_v5;
DROP TYPE IF EXISTS memory.projection_review_state_v5;
DROP TYPE IF EXISTS memory.projection_target_action_v5;
DROP TYPE IF EXISTS memory.projection_lane_v5;

REVOKE SELECT ON
  memory.claim,
  memory.claim_revision,
  memory.user_preference,
  memory.preference_revision,
  memory.project_space,
  memory.project_knowledge_head,
  memory.project_knowledge_revision
FROM memory_v5_writer;

COMMIT;
