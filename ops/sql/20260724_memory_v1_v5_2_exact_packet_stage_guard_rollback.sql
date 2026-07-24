BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

CREATE OR REPLACE FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  extraction jsonb;
  source_job_id uuid;
  source_packet_id uuid;
  authoritative_packet_id uuid;
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

  SELECT packet.packet_id INTO source_packet_id
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=NEW.owner_user_id
    AND packet.evidence_id=NEW.evidence_id
    AND packet.job_id=source_job_id;
  IF source_packet_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not owner-authoritative'
      USING ERRCODE='23514';
  END IF;

  authoritative_packet_id:=
    memory.authoritative_owner_v5_2_packet_id_v1(NEW.evidence_id);
  IF authoritative_packet_id IS NULL
     OR source_packet_id<>authoritative_packet_id THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not the authority leaf'
      USING ERRCODE='23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet_id
      AND event.route='manual_review_artifact_ready'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet_id
      AND event.route='terminal_no_stage'
  ) THEN
    RAISE EXCEPTION 'V5.2 stage source packet lacks an active review route'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
  OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1()
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;

COMMENT ON FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1()
IS 'Allows V5.2 staging only from the provenance-authoritative leaf packet with an append-only manual-review route; superseded, ambiguous, terminal, and unrouted packets fail closed.';

COMMIT;
