BEGIN;

DO $guard$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5.2 projection dispatch rollback must run as sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE predicate_registry_version='memory_predicate_registry_v5_2'
  ) THEN
    RAISE EXCEPTION 'V5.2 projection plans exist; rollback refused';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.stage_projection_plan_v5_2(uuid,text,text);
DROP FUNCTION IF EXISTS memory.preflight_projection_packet_v5_2(uuid,text);
DROP FUNCTION IF EXISTS memory.expected_projection_payload_v5_2(uuid);
DROP FUNCTION IF EXISTS memory.preflight_projection_source_v5_2(uuid);
DROP FUNCTION IF EXISTS memory.render_projection_claim_text_v5_2(uuid);
DROP FUNCTION IF EXISTS memory.v5_2_projection_sentence(
  text,text,text,memory.observation_polarity,memory.observation_modality
);

ALTER TABLE memory.projection_plan
  DROP CONSTRAINT IF EXISTS projection_plan_predicate_registry_version_check;
ALTER TABLE memory.projection_plan
  ADD CONSTRAINT projection_plan_predicate_registry_version_check CHECK (
    predicate_registry_version IN (
      'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
    )
  );

ALTER TABLE memory.projection_plan_item
  DROP CONSTRAINT IF EXISTS projection_plan_item_predicate_registry_version_check;
ALTER TABLE memory.projection_plan_item
  ADD CONSTRAINT projection_plan_item_predicate_registry_version_check CHECK (
    predicate_registry_version IN (
      'memory_predicate_registry_v5','memory_predicate_registry_v5_1'
    )
  );

ALTER TABLE memory.projection_claim_payload
  DROP CONSTRAINT IF EXISTS projection_claim_payload_claim_class_check;
ALTER TABLE memory.projection_claim_payload
  ADD CONSTRAINT projection_claim_payload_claim_class_check CHECK (
    claim_class IN (
      'direct_claim','supportive_context','correction','never_surface'
    )
  );

COMMIT;
