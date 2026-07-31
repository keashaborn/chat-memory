BEGIN;

DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid);

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.evidence;
DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.evidence_extraction_packet_v5_local;
DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.v5_2_local_packet_route_event;
DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.entity_resolution_plan;
DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.entity_mention;
DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.observation;

REVOKE memory_v5_manual_packet_review_reader FROM sage;
DROP OWNED BY memory_v5_manual_packet_review_reader;
DROP ROLE IF EXISTS memory_v5_manual_packet_review_reader;

COMMIT;
