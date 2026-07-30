BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regrole('memory_v5_2_local_router_maintainer') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'
     ) IS NULL
     OR to_regprocedure('memory.guard_v5_legacy_packet_lane_v1()') IS NULL
     OR to_regprocedure(
       'memory.guard_disposed_evidence_from_stage_v1()'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 review disposition prerequisites are absent';
  END IF;
END
$preflight$;

ALTER TABLE memory.v5_local_packet_disposition
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_reason_code_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_review_contract_check;

ALTER TABLE memory.v5_local_packet_disposition
  ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
    CHECK (reason_code IN (
      'deferral_only_no_stage',
      'deferral_only_review_unresolved',
      'ambiguous_transcription',
      'semantic_predicate_misclassification',
      'turn_local_instruction_not_durable',
      'hypothetical_or_scenario_not_durable',
      'contextual_state_not_durable'
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
      ) OR (
        reason_code IN (
          'semantic_predicate_misclassification',
          'turn_local_instruction_not_durable',
          'hypothetical_or_scenario_not_durable'
        )
        AND review_decision='rejected'
        AND review_basis_sha256 ~ '^[0-9a-f]{64}$'
        AND entity_mention_count+observation_count
          +comparison_hint_count+deferral_count BETWEEN 1 AND 128
      ) OR (
        reason_code='contextual_state_not_durable'
        AND review_decision='deferred'
        AND review_basis_sha256 ~ '^[0-9a-f]{64}$'
        AND entity_mention_count+observation_count
          +comparison_hint_count+deferral_count BETWEEN 1 AND 128
      )
    );

CREATE OR REPLACE FUNCTION
memory.finalize_owner_v5_2_review_disposition_v1(
  p_operation_id uuid,
  p_disposition_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_review_decision text,
  p_reason_code text,
  p_review_basis_sha256 text
)
RETURNS TABLE(
  disposition_id uuid,
  packet_id uuid,
  disposition text,
  review_decision text,
  reason_code text,
  promotion_eligible boolean,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  packet record;
  replayed memory.v5_local_packet_disposition%ROWTYPE;
  calculated_storage_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 review disposition requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_disposition_id IS NULL
     OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR NOT (
       (
         p_review_decision='rejected'
         AND p_reason_code IN (
           'semantic_predicate_misclassification',
           'turn_local_instruction_not_durable',
           'hypothetical_or_scenario_not_durable'
         )
       ) OR (
         p_review_decision='deferred'
         AND p_reason_code='contextual_state_not_durable'
       )
     ) THEN
    RAISE EXCEPTION 'V5.2 review disposition inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_review_disposition',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_disposition AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.disposition_id<>p_disposition_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.disposition<>'terminal_no_stage'
       OR replayed.review_decision<>p_review_decision
       OR replayed.reason_code<>p_reason_code
       OR replayed.promotion_eligible
       OR replayed.review_basis_sha256<>p_review_basis_sha256 THEN
      RAISE EXCEPTION 'V5.2 review disposition replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.disposition_id,replayed.packet_id,
      replayed.disposition,replayed.review_decision,replayed.reason_code,
      replayed.promotion_eligible,'replayed'::text;
    RETURN;
  END IF;

  SELECT
    local_packet.*,
    job.status::text AS job_status,
    job.route AS job_route,
    job.lease_token,
    job.lease_expires_at,
    job.last_error,
    evidence.status::text AS evidence_status,
    evidence.content_sha256 AS authority_sha256
  INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS local_packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=local_packet.owner_user_id
   AND job.job_id=local_packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=local_packet.owner_user_id
   AND evidence.evidence_id=local_packet.evidence_id
  JOIN memory.v5_2_local_packet_route_event AS route
    ON route.owner_user_id=local_packet.owner_user_id
   AND route.packet_id=local_packet.packet_id
   AND route.route='manual_review_artifact_ready'
  WHERE local_packet.owner_user_id=actor
    AND local_packet.packet_id=p_packet_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped reviewed V5.2 packet is absent'
      USING ERRCODE='23514';
  END IF;

  calculated_storage_sha256:=encode(public.digest(convert_to(
    packet.normalized_packet::text,'UTF8'
  ),'sha256'),'hex');
  IF packet.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR calculated_storage_sha256<>packet.packet_storage_sha256
     OR packet.validator_packet_sha256 IS NULL
     OR packet.normalized_packet->>'contract_version'
          <>'memory_v1_relational_extraction_v5_2'
     OR packet.normalized_packet->>'predicate_registry_version'
          <>'memory_predicate_registry_v5_2'
     OR packet.provider_id<>'local_llama_cpp'
     OR packet.local_model_calls NOT BETWEEN 0 AND 1
     OR packet.external_model_calls<>0
     OR jsonb_typeof(packet.normalized_packet)<>'object'
     OR jsonb_array_length(packet.normalized_packet->'entity_mentions')
          <>packet.entity_mention_count
     OR jsonb_array_length(packet.normalized_packet->'observations')
          <>packet.observation_count
     OR jsonb_array_length(packet.normalized_packet->'comparison_hints')
          <>packet.comparison_hint_count
     OR jsonb_array_length(packet.normalized_packet->'deferrals')
          <>packet.deferral_count
     OR packet.entity_mention_count+packet.observation_count
          +packet.comparison_hint_count+packet.deferral_count<1
     OR packet.job_status<>'review_required'
     OR packet.job_route<>'relational_extraction'
     OR packet.lease_token IS NOT NULL
     OR packet.lease_expires_at IS NOT NULL
     OR packet.last_error IS NOT NULL
     OR packet.evidence_status<>'active'
     OR packet.authority_sha256<>packet.evidence_content_sha256
     OR memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id)
          IS DISTINCT FROM packet.packet_id
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id=actor
         AND stage.evidence_id=packet.evidence_id
     ) THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 review disposition'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_disposition(
    disposition_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    disposition,reason_code,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,
    entity_mention_count,observation_count,comparison_hint_count,
    deferral_count,local_model_calls,external_model_calls,
    review_decision,promotion_eligible,review_basis_sha256
  ) VALUES (
    p_disposition_id,actor,p_operation_id,p_packet_id,packet.job_id,
    packet.evidence_id,'terminal_no_stage',p_reason_code,
    packet.evidence_content_sha256,packet.validator_packet_sha256,
    packet.packet_storage_sha256,packet.entity_mention_count,
    packet.observation_count,packet.comparison_hint_count,
    packet.deferral_count,packet.local_model_calls,packet.external_model_calls,
    p_review_decision,false,p_review_basis_sha256
  );

  RETURN QUERY SELECT p_disposition_id,p_packet_id,'terminal_no_stage'::text,
    p_review_decision,p_reason_code,false,'applied'::text;
END
$function$;

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
            AND NOT NEW.promotion_eligible
            AND NEW.review_basis_sha256 ~ '^[0-9a-f]{64}$'
            AND (
              (
                NEW.review_decision='deferred'
                AND NEW.reason_code IN (
                  'ambiguous_transcription',
                  'contextual_state_not_durable'
                )
              ) OR (
                NEW.review_decision='rejected'
                AND NEW.reason_code IN (
                  'semantic_predicate_misclassification',
                  'turn_local_instruction_not_durable',
                  'hypothetical_or_scenario_not_durable'
                )
              )
            )
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

ALTER FUNCTION memory.finalize_owner_v5_2_review_disposition_v1(
  uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  OWNER TO memory_v5_local_disposition_maintainer;

GRANT SELECT ON memory.v5_2_local_packet_route_event
  TO memory_v5_local_disposition_maintainer;
DROP POLICY IF EXISTS v5_2_review_disposition_route_read
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY v5_2_review_disposition_route_read
  ON memory.v5_2_local_packet_route_event
  FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
GRANT EXECUTE ON FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  TO memory_v5_local_disposition_maintainer;

REVOKE ALL ON FUNCTION
memory.finalize_owner_v5_2_review_disposition_v1(
  uuid,uuid,uuid,text,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION
memory.finalize_owner_v5_2_review_disposition_v1(
  uuid,uuid,uuid,text,text,text,text
) TO brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;

COMMENT ON FUNCTION memory.finalize_owner_v5_2_review_disposition_v1(
  uuid,uuid,uuid,text,text,text,text
) IS 'Records a reviewed owner-scoped V5.2 packet as rejected or deferred, append-only and permanently promotion-ineligible.';

COMMIT;
