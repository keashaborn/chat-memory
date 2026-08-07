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
  source_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  authoritative_packet_id uuid;
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
  EXCEPTION
    WHEN OTHERS THEN
      RAISE EXCEPTION 'relational stage extraction packet is invalid JSON'
        USING ERRCODE='23514';
  END;

  IF extraction->>'contract_version'
       <>'memory_v1_relational_extraction_v5_2' THEN
    IF EXISTS (
      SELECT 1
      FROM memory.v5_2_local_packet_route_event AS event
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
  EXCEPTION
    WHEN OTHERS THEN
      RAISE EXCEPTION 'V5.2 stage packet has no valid source job'
        USING ERRCODE='23514';
  END;

  SELECT packet.* INTO source_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=NEW.owner_user_id
    AND packet.evidence_id=NEW.evidence_id
    AND packet.job_id=source_job_id;
  IF source_packet.packet_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not owner-authoritative'
      USING ERRCODE='23514';
  END IF;

  exact_packet:=extraction IS NOT DISTINCT FROM source_packet.normalized_packet
    AND NEW.extraction_packet_sha256 IS NOT DISTINCT FROM
      source_packet.validator_packet_sha256;
  atom_projection:=memory.v5_2_atom_stage_projection_authorized_v1(
    NEW.owner_user_id,NEW.evidence_id,extraction,
    NEW.extraction_packet_sha256
  );
  IF NOT exact_packet AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 stage packet differs from immutable extraction or authorized atom projection'
      USING ERRCODE='23514';
  END IF;

  authoritative_packet_id:=
    memory.authoritative_owner_v5_2_packet_id_v1(NEW.evidence_id);
  IF authoritative_packet_id IS NULL
     OR source_packet.packet_id<>authoritative_packet_id THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not the authority leaf'
      USING ERRCODE='23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet.packet_id
      AND event.route='manual_review_artifact_ready'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet.packet_id
      AND event.route='terminal_no_stage'
  ) THEN
    RAISE EXCEPTION 'V5.2 stage source packet lacks an active review route'
      USING ERRCODE='23514';
  END IF;

  IF (
       source_packet.deferral_count>0
       OR jsonb_array_length(source_packet.normalized_packet->'deferrals')>0
     ) AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 packet deferrals require controlled atom-level admission'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;
ALTER FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1() OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1() FROM PUBLIC;
