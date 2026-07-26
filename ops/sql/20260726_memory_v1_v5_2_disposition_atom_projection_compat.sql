\set ON_ERROR_STOP on
BEGIN;

DO $install$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION
      'V5.2 disposition compatibility install requires sage'
      USING ERRCODE='42501';
  END IF;
  IF to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regprocedure(
       'memory.guard_disposed_evidence_from_stage_v1()'
     ) IS NULL
     OR to_regprocedure(
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)'
     ) IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL THEN
    RAISE EXCEPTION
      'V5.2 disposition compatibility prerequisites are absent'
      USING ERRCODE='55000';
  END IF;
END
$install$;

CREATE OR REPLACE FUNCTION memory.guard_disposed_evidence_from_stage_v2()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  extraction jsonb;
  exact_admitted_atom boolean := false;
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'stage disposition guard accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id THEN
    RAISE EXCEPTION 'stage disposition guard requires exact owner context'
      USING ERRCODE='42501';
  END IF;

  IF NOT EXISTS (
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
    RETURN NEW;
  END IF;

  BEGIN
    extraction:=NEW.extraction_packet_text::jsonb;
  EXCEPTION
    WHEN OTHERS THEN
      RAISE EXCEPTION
        'disposed evidence requires a valid governed atom projection'
        USING ERRCODE='23514';
  END;

  exact_admitted_atom:=
    memory.v5_2_atom_stage_projection_authorized_v1(
      NEW.owner_user_id,
      NEW.evidence_id,
      extraction,
      NEW.extraction_packet_sha256
    );
  IF NOT exact_admitted_atom THEN
    RAISE EXCEPTION 'disposed evidence cannot enter relational staging'
      USING ERRCODE='23514';
  END IF;

  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.guard_disposed_evidence_from_stage_v2()
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.guard_disposed_evidence_from_stage_v2()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;

DROP TRIGGER IF EXISTS v5_local_disposition_stage_guard
  ON memory.relational_stage_batch;
CREATE TRIGGER v5_local_disposition_stage_guard
BEFORE INSERT ON memory.relational_stage_batch
FOR EACH ROW
EXECUTE FUNCTION memory.guard_disposed_evidence_from_stage_v2();

COMMENT ON FUNCTION memory.guard_disposed_evidence_from_stage_v2()
IS 'Preserves terminal packet dispositions while allowing only an exact V5.2 atom projection already authorized by the governed admission ledger.';

COMMIT;
