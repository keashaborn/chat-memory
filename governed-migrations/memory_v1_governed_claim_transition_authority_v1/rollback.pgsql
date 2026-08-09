REVOKE ALL ON memory.read_governed_claim_supported_held_v1
  FROM PUBLIC,brains_app;
REVOKE ALL ON memory.read_governed_claim_assessment_apply_ready_v1
  FROM PUBLIC,brains_app;
REVOKE ALL ON memory.read_governed_claim_materialization_authority_v1
  FROM PUBLIC,brains_app;
REVOKE ALL ON memory.read_governed_claim_projection_apply_ready_v1
  FROM PUBLIC,brains_app;

DROP VIEW memory.read_governed_claim_supported_held_v1;
DROP VIEW memory.read_governed_claim_assessment_apply_ready_v1;
DROP VIEW memory.read_governed_claim_materialization_authority_v1;
DROP VIEW memory.read_governed_claim_projection_apply_ready_v1;

REVOKE EXECUTE ON FUNCTION
  memory.v5_sha256_valid(text),
  memory.v5_digest_text(text),
  memory.v5_canonical_json_text(jsonb),
  memory.v5_projection_owner_manifest_sha256(uuid,text),
  memory.v5_projection_semantic_key_sha256(
    uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,
    memory.observation_polarity,memory.observation_modality,jsonb
  ),
  memory.v5_projection_review_manifest_sha256(
    uuid,uuid,text,text,text,integer,memory.projection_review_decision_v5,
    text,text,text,jsonb
  ),
  memory.v5_projection_apply_manifest_sha256(
    uuid,text,uuid,text,text,text,memory.projection_lane_v5,
    memory.projection_target_action_v5,integer,uuid,text
  )
FROM brains_app;

REVOKE SELECT ON memory.projection_outbox FROM memory_v5_writer;
