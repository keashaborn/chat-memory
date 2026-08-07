CREATE OR REPLACE FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  extraction jsonb;
  source_job_id uuid;
  source_packet record;
  source_count integer;
  authoritative_packet_id uuid;
  external_leaf_count integer;
  exact_packet boolean;
  atom_projection boolean;
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage guard accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id THEN
    RAISE EXCEPTION 'V5.2 stage guard requires exact owner context'
      USING ERRCODE='42501';
  END IF;
  BEGIN
    extraction:=NEW.extraction_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'relational stage extraction packet is invalid JSON'
      USING ERRCODE='23514';
  END;
  IF extraction->>'contract_version'
       <>'memory_v1_relational_extraction_v5_2' THEN
    IF EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS event
      WHERE event.owner_user_id=NEW.owner_user_id
        AND event.evidence_id=NEW.evidence_id
        AND event.route='terminal_no_stage'
    ) THEN
      RAISE EXCEPTION 'terminal V5.2 evidence cannot enter relational staging'
        USING ERRCODE='23514';
    END IF;
    RETURN NEW;
  END IF;
  BEGIN
    source_job_id:=(extraction#>>'{source_envelope,job_id}')::uuid;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'V5.2 stage packet has no valid source job'
      USING ERRCODE='23514';
  END;
  SELECT count(*) INTO source_count
  FROM (
    SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id=NEW.owner_user_id AND evidence_id=NEW.evidence_id
      AND job_id=source_job_id
    UNION ALL
    SELECT packet_id FROM memory.evidence_extraction_packet_v5
    WHERE owner_user_id=NEW.owner_user_id AND evidence_id=NEW.evidence_id
      AND job_id=source_job_id
  ) AS source;
  IF source_count<>1 THEN
    RAISE EXCEPTION 'V5.2 stage source packet is absent or ambiguous'
      USING ERRCODE='23514';
  END IF;
  SELECT source.* INTO STRICT source_packet
  FROM (
    SELECT 'local'::text AS source_kind,packet_id,job_id,evidence_id,
      validator_packet_sha256,packet_storage_sha256,normalized_packet,
      deferral_count,provider_id,NULL::text AS provider_version,
      external_model_calls
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id=NEW.owner_user_id AND evidence_id=NEW.evidence_id
      AND job_id=source_job_id
    UNION ALL
    SELECT 'openai'::text,packet_id,job_id,evidence_id,
      validator_packet_sha256,packet_storage_sha256,normalized_packet,
      deferral_count,provider_id,provider_version,external_model_calls
    FROM memory.evidence_extraction_packet_v5
    WHERE owner_user_id=NEW.owner_user_id AND evidence_id=NEW.evidence_id
      AND job_id=source_job_id
  ) AS source;
  IF source_packet.source_kind='openai' AND (
       source_packet.provider_id<>'openai_responses'
       OR source_packet.provider_version<>'v1'
       OR source_packet.external_model_calls<>1
       OR source_packet.normalized_packet->>'predicate_registry_version'
            <>'memory_predicate_registry_v5_2'
     ) THEN
    RAISE EXCEPTION 'OpenAI V5.2 stage provenance is invalid'
      USING ERRCODE='23514';
  END IF;
  exact_packet:=extraction IS NOT DISTINCT FROM source_packet.normalized_packet
    AND NEW.extraction_packet_sha256 IS NOT DISTINCT FROM
      source_packet.validator_packet_sha256;
  atom_projection:=source_packet.source_kind='local' AND
    memory.v5_2_atom_stage_projection_authorized_v1(
      NEW.owner_user_id,NEW.evidence_id,extraction,
      NEW.extraction_packet_sha256
    );
  IF NOT exact_packet AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 stage packet differs from immutable extraction or authorized atom projection'
      USING ERRCODE='23514';
  END IF;
  IF source_packet.source_kind='local' THEN
    authoritative_packet_id:=
      memory.authoritative_owner_v5_2_packet_id_v1(NEW.evidence_id);
    IF authoritative_packet_id IS NULL
       OR source_packet.packet_id<>authoritative_packet_id THEN
      RAISE EXCEPTION 'V5.2 stage source packet is not the authority leaf'
        USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS event
      WHERE event.owner_user_id=NEW.owner_user_id
        AND event.evidence_id=NEW.evidence_id
        AND event.packet_id=source_packet.packet_id
        AND event.route='manual_review_artifact_ready'
    ) OR EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS event
      WHERE event.owner_user_id=NEW.owner_user_id
        AND event.evidence_id=NEW.evidence_id
        AND event.packet_id=source_packet.packet_id
        AND event.route='terminal_no_stage'
    ) THEN
      RAISE EXCEPTION 'V5.2 stage source packet lacks an active review route'
        USING ERRCODE='23514';
    END IF;
  ELSE
    SELECT count(*) INTO external_leaf_count
    FROM memory.evidence_extraction_packet_v5 AS packet
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id AND job.job_id=packet.job_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    WHERE packet.owner_user_id=NEW.owner_user_id
      AND packet.evidence_id=NEW.evidence_id
      AND packet.provider_id='openai_responses'
      AND packet.provider_version='v1'
      AND packet.external_model_calls=1
      AND packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5_2'
      AND job.status::text='review_required'
      AND job.route='relational_extraction'
      AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL AND evidence.status::text='active'
      AND evidence.content_sha256=packet.evidence_content_sha256
      AND NOT EXISTS (
        SELECT 1 FROM memory.evidence_extraction_event AS lineage
        JOIN memory.evidence_extraction_job AS successor
          ON successor.owner_user_id=lineage.owner_user_id
         AND successor.job_id=lineage.job_id
        WHERE lineage.owner_user_id=packet.owner_user_id
          AND lineage.event_type='queued'
          AND lineage.details->>'prior_packet_id'=packet.packet_id::text
          AND successor.evidence_id=packet.evidence_id
          AND successor.evidence_content_sha256=packet.evidence_content_sha256
          AND successor.route='relational_extraction'
      );
    IF external_leaf_count<>1 OR NOT EXISTS (
      SELECT 1 FROM memory.v5_2_openai_packet_route_event AS event
      WHERE event.owner_user_id=NEW.owner_user_id
        AND event.evidence_id=NEW.evidence_id
        AND event.packet_id=source_packet.packet_id
        AND event.route='manual_review_artifact_ready'
        AND event.review_contract='memory_v1_v5_2_openai_packet_review_v1'
        AND event.bundle_contract='memory_v1_v5_2_stage_preflight_v1'
        AND event.packet_storage_sha256=source_packet.packet_storage_sha256
        AND event.validator_packet_sha256=source_packet.validator_packet_sha256
        AND event.entity_mention_count=
              jsonb_array_length(source_packet.normalized_packet->'entity_mentions')
        AND event.observation_count=
              jsonb_array_length(source_packet.normalized_packet->'observations')
        AND event.comparison_hint_count=
              jsonb_array_length(source_packet.normalized_packet->'comparison_hints')
        AND event.deferral_count=source_packet.deferral_count
    ) THEN
      RAISE EXCEPTION 'OpenAI V5.2 stage source lacks exact route authority'
        USING ERRCODE='23514';
    END IF;
  END IF;
  IF source_packet.deferral_count>0 AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 packet deferrals require controlled atom-level admission'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;
ALTER FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1() OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1() FROM PUBLIC;
