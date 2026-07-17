BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection apply V5 rollback must run as sage, current_user=%',
      current_user;
  END IF;
  IF EXISTS (SELECT 1 FROM memory.claim_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.preference_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_relation_v5)
     OR EXISTS (SELECT 1 FROM memory.projection_dispatch_v5)
     OR EXISTS (SELECT 1 FROM memory.projection_review)
     OR EXISTS (SELECT 1 FROM memory.projection_apply_event) THEN
    RAISE EXCEPTION
      'projection apply rollback requires empty review, apply, relation, and dispatch tables';
  END IF;
END
$block$;

REVOKE EXECUTE ON FUNCTION memory.preflight_projection_review_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb
) FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.review_projection_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb, text
) FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.preflight_projection_apply_v5(
  uuid, text, uuid
) FROM brains_app;
REVOKE EXECUTE ON FUNCTION memory.apply_projection_v5(
  uuid, uuid, text, uuid, text
) FROM brains_app;

REVOKE INSERT ON
  memory.claim,
  memory.claim_revision,
  memory.claim_observation
FROM memory_v5_writer;
REVOKE UPDATE (canonical_text, retrieval_policy, metadata)
  ON memory.claim FROM memory_v5_writer;

DROP FUNCTION memory.apply_projection_v5(uuid, uuid, text, uuid, text);
DROP FUNCTION memory.preflight_projection_apply_v5(uuid, text, uuid);
DROP FUNCTION memory.review_projection_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb, text
);
DROP FUNCTION memory.preflight_projection_review_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb
);
DROP FUNCTION memory.projection_apply_state_v5(uuid, text, uuid);
DROP FUNCTION memory.v5_projection_apply_manifest_sha256(
  uuid, text, uuid, text, text, text, memory.projection_lane_v5,
  memory.projection_target_action_v5, integer, uuid, text
);
DROP FUNCTION memory.v5_projection_review_manifest_sha256(
  uuid, uuid, text, text, text, integer,
  memory.projection_review_decision_v5, text, text, text, jsonb
);

DROP TABLE memory.projection_dispatch_v5;
DROP TABLE memory.project_knowledge_relation_v5;
DROP TABLE memory.preference_relation_v5;
DROP TABLE memory.claim_relation_v5;

COMMIT;
