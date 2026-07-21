BEGIN;

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.projection_plan
    WHERE projector='memory_v1_deterministic_life_event_claim_projection_v5_1'
  ) THEN
    RAISE EXCEPTION 'life-event claim projection rollback refused: durable plans exist';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.preflight_claim_projection_packet_v5_1(
  uuid,text
);
DROP FUNCTION IF EXISTS memory.preflight_claim_projection_source_v5_1(uuid);
DROP FUNCTION IF EXISTS memory.render_life_event_claim_text_v5_1(
  text,text,jsonb,text,jsonb
);

ALTER FUNCTION memory.preflight_claim_projection_source_v5_1_base(uuid)
  RENAME TO preflight_claim_projection_source_v5_1;
ALTER FUNCTION memory.preflight_claim_projection_packet_v5_1_base(uuid,text)
  RENAME TO preflight_claim_projection_packet_v5_1;

REVOKE ALL ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_projection_packet_v5_1(uuid,text)
  TO brains_app;

COMMIT;
