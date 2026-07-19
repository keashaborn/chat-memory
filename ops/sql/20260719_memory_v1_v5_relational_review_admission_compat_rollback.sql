BEGIN;

DO $rollback$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)';
  definition text;
  anchor text :=
    E'     OR packet.local_model_calls<>1 OR packet.external_model_calls<>0\n';
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'relational review admission rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_review_artifact AS artifact
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=artifact.owner_user_id
     AND packet.packet_id=artifact.packet_id
    WHERE NOT packet.manual_review_required
  ) THEN
    RAISE EXCEPTION 'rollback refused: accepted relational packet artifacts exist';
  END IF;

  definition := pg_get_functiondef(function_oid);
  IF position('NOT packet.manual_review_required' IN definition)=0 THEN
    IF position(anchor IN definition)=0 THEN
      RAISE EXCEPTION 'local review artifact writer has an unknown definition';
    END IF;
    EXECUTE replace(
      definition,
      anchor,
      anchor || E'     OR NOT packet.manual_review_required\n'
    );
  END IF;
END
$rollback$;

COMMIT;
