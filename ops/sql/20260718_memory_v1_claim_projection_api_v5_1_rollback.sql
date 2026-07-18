BEGIN;

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE projector='memory_v1_deterministic_claim_projection_v5_1'
      AND projector_version='claim_template_v1'
  ) THEN
    RAISE EXCEPTION 'V5.1 claim projection API has durable plans; rollback refused';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.stage_claim_projection_plan_v5_1(
  uuid,text,text
);
DROP FUNCTION IF EXISTS memory.preflight_claim_projection_packet_v5_1(
  uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_claim_projection_source_v5_1(uuid);
DROP FUNCTION IF EXISTS memory.render_claim_projection_text_v5_1(
  text,text,text,text,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.v5_claim_literal_value_v5_1(text,jsonb);

COMMIT;
