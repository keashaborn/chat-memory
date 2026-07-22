BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
DECLARE
  persistence_definition text;
  old_compiler_guard constant text :=
$old$OR p_policy_compiler_sha256 <> encode(public.digest(convert_to(
       'memory_v1_semantic_policy_compiler_v1','UTF8'
     ),'sha256'),'hex')$old$;
  new_compiler_guard constant text :=
$new$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex')
     )$new$;
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_local_inference_append_only()') IS NULL
     OR to_regprocedure(
       'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 ambiguous review deferral prerequisites are absent';
  END IF;

  SELECT pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ) INTO persistence_definition;
  IF strpos(persistence_definition,'memory_v1_semantic_policy_compiler_v2')=0 THEN
    IF strpos(persistence_definition,old_compiler_guard)=0 THEN
      RAISE EXCEPTION 'V5.2 persistence compiler guard drifted';
    END IF;
    persistence_definition:=replace(
      persistence_definition,old_compiler_guard,new_compiler_guard
    );
    EXECUTE persistence_definition;
  ELSIF strpos(persistence_definition,new_compiler_guard)=0 THEN
    RAISE EXCEPTION 'V5.2 persistence compiler compatibility is inconsistent';
  END IF;
END
$preflight$;

ALTER TABLE memory.v5_local_packet_disposition
  ADD COLUMN IF NOT EXISTS review_decision text,
  ADD COLUMN IF NOT EXISTS promotion_eligible boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS review_basis_sha256 text;

ALTER TABLE memory.v5_local_packet_disposition
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_reason_code_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_entity_mention_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_observation_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_comparison_hint_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_deferral_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_promotion_eligible_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_review_contract_check;

ALTER TABLE memory.v5_local_packet_disposition
  ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
    CHECK (reason_code IN (
      'deferral_only_no_stage',
      'deferral_only_review_unresolved',
      'ambiguous_transcription'
    )),
  ADD CONSTRAINT v5_local_packet_disposition_entity_mention_count_check
    CHECK (entity_mention_count BETWEEN 0 AND 32),
  ADD CONSTRAINT v5_local_packet_disposition_observation_count_check
    CHECK (observation_count BETWEEN 0 AND 32),
  ADD CONSTRAINT v5_local_packet_disposition_comparison_hint_count_check
    CHECK (comparison_hint_count BETWEEN 0 AND 32),
  ADD CONSTRAINT v5_local_packet_disposition_deferral_count_check
    CHECK (deferral_count BETWEEN 0 AND 32),
  ADD CONSTRAINT v5_local_packet_disposition_promotion_eligible_check
    CHECK (NOT promotion_eligible),
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

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_2_review_deferral_v1(
  p_operation_id uuid,
  p_disposition_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
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
    RAISE EXCEPTION 'V5.2 review deferral requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_disposition_id IS NULL OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_code<>'ambiguous_transcription'
     OR p_review_basis_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'V5.2 review deferral inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_review_deferral',actor::text,p_packet_id::text
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
       OR replayed.review_decision<>'deferred'
       OR replayed.reason_code<>p_reason_code
       OR replayed.promotion_eligible
       OR replayed.review_basis_sha256<>p_review_basis_sha256 THEN
      RAISE EXCEPTION 'V5.2 review deferral replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.disposition_id,replayed.packet_id,
      replayed.disposition,replayed.review_decision,replayed.reason_code,
      replayed.promotion_eligible,'replayed'::text;
    RETURN;
  END IF;

  SELECT local_packet.*,
    job.status::text AS job_status,job.route AS job_route,
    job.lease_token,job.lease_expires_at,job.last_error,
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
  WHERE local_packet.owner_user_id=actor
    AND local_packet.packet_id=p_packet_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped V5.2 packet is absent'
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
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id=actor
         AND stage.evidence_id=packet.evidence_id
     ) THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 review deferral'
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
    'deferred',false,p_review_basis_sha256
  );

  RETURN QUERY SELECT p_disposition_id,p_packet_id,'terminal_no_stage'::text,
    'deferred'::text,p_reason_code,false,'applied'::text;
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

CREATE OR REPLACE FUNCTION memory.guard_disposed_evidence_from_stage_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'stage disposition guard accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id THEN
    RAISE EXCEPTION 'stage disposition guard requires exact owner context'
      USING ERRCODE='42501';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_disposition AS disposition
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=disposition.owner_user_id
     AND packet.packet_id=disposition.packet_id
    WHERE disposition.owner_user_id=NEW.owner_user_id
      AND packet.evidence_id=NEW.evidence_id
      AND disposition.disposition='terminal_no_stage'
      AND NOT disposition.promotion_eligible
  ) THEN
    RAISE EXCEPTION 'disposed evidence cannot enter relational staging'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.finalize_owner_v5_2_review_deferral_v1(
  uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.guard_disposed_evidence_from_stage_v1()
  OWNER TO memory_v5_local_disposition_maintainer;

REVOKE ALL ON FUNCTION memory.finalize_owner_v5_2_review_deferral_v1(
  uuid,uuid,uuid,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_2_review_deferral_v1(
  uuid,uuid,uuid,text,text,text
) TO brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.guard_disposed_evidence_from_stage_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;

DROP TRIGGER IF EXISTS v5_local_disposition_stage_guard
  ON memory.relational_stage_batch;
CREATE TRIGGER v5_local_disposition_stage_guard
BEFORE INSERT ON memory.relational_stage_batch
FOR EACH ROW EXECUTE FUNCTION memory.guard_disposed_evidence_from_stage_v1();

COMMENT ON FUNCTION memory.finalize_owner_v5_2_review_deferral_v1(
  uuid,uuid,uuid,text,text,text
) IS 'Records an owner-scoped, append-only V5.2 review deferral. The immutable packet remains evidence; it is promotion-ineligible and cannot enter relational staging.';

COMMIT;
