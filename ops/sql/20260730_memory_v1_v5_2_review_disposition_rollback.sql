BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 review disposition rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_disposition
    WHERE reason_code IN (
      'semantic_predicate_misclassification',
      'turn_local_instruction_not_durable',
      'hypothetical_or_scenario_not_durable',
      'contextual_state_not_durable'
    )
  ) THEN
    RAISE EXCEPTION 'review disposition rows exist; rollback is unsafe';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.finalize_owner_v5_2_review_disposition_v1(
  uuid,uuid,uuid,text,text,text,text
);

ALTER TABLE memory.v5_local_packet_disposition
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_reason_code_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_review_contract_check;

ALTER TABLE memory.v5_local_packet_disposition
  ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
    CHECK (reason_code IN (
      'deferral_only_no_stage',
      'deferral_only_review_unresolved',
      'ambiguous_transcription'
    )),
  ADD CONSTRAINT v5_local_packet_disposition_review_contract_check
    CHECK (
      (
        reason_code IN (
          'deferral_only_no_stage','deferral_only_review_unresolved'
        )
        AND review_decision IS NULL
        AND review_basis_sha256 IS NULL
        AND entity_mention_count=0
        AND observation_count=0
        AND comparison_hint_count=0
        AND deferral_count BETWEEN 1 AND 32
      ) OR (
        reason_code='ambiguous_transcription'
        AND review_decision='deferred'
        AND review_basis_sha256 ~ '^[0-9a-f]{64}$'
        AND entity_mention_count+observation_count
          +comparison_hint_count+deferral_count BETWEEN 1 AND 128
      )
    );

CREATE OR REPLACE FUNCTION memory.guard_v5_legacy_packet_lane_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.packet_id IS NULL THEN
    RAISE EXCEPTION 'packet lane accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='v5_local_packet_disposition' THEN
    IF NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id=NEW.owner_user_id
        AND packet.packet_id=NEW.packet_id
        AND (
          (
            packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5'
            AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5'
          ) OR (
            packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5_2'
            AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5_2'
            AND NEW.reason_code='ambiguous_transcription'
            AND NEW.review_decision='deferred'
            AND NOT NEW.promotion_eligible
            AND NEW.review_basis_sha256 ~ '^[0-9a-f]{64}$'
          )
        )
    ) THEN
      RAISE EXCEPTION 'packet is not eligible for the disposition lane'
        USING ERRCODE='23514';
    END IF;
  ELSIF TG_TABLE_NAME='v5_local_packet_review_artifact' THEN
    IF NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id=NEW.owner_user_id
        AND packet.packet_id=NEW.packet_id
        AND packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5'
        AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5'
    ) THEN
      RAISE EXCEPTION 'non-V5 packet cannot enter the legacy review lane'
        USING ERRCODE='23514';
    END IF;
  ELSE
    RAISE EXCEPTION 'unexpected packet lane trigger target'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
DROP POLICY IF EXISTS v5_2_review_disposition_route_read
  ON memory.v5_2_local_packet_route_event;
REVOKE SELECT ON memory.v5_2_local_packet_route_event
  FROM memory_v5_local_disposition_maintainer;
REVOKE EXECUTE ON FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  FROM memory_v5_local_disposition_maintainer;

COMMIT;
