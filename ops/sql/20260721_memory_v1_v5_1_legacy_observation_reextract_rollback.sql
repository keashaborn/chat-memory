BEGIN;

DROP POLICY IF EXISTS v5_1_legacy_observation_reextract_read
  ON memory.observation;
DROP POLICY IF EXISTS v5_1_legacy_packet_reextract_read
  ON memory.evidence_extraction_packet_v5_local;

DROP FUNCTION IF EXISTS
  memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
    uuid,text,text,text,text,integer
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_1_legacy_observation_reextract_v1(text,integer,uuid);

COMMIT;
