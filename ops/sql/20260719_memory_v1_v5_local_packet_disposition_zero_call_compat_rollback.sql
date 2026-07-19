BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
DECLARE
  function_definition text;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'zero-call disposition compatibility rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_packet_disposition
    WHERE local_model_calls=0
  ) THEN
    RAISE EXCEPTION 'cannot restore strict constraint while zero-call rows exist';
  END IF;

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO function_definition;
  IF strpos(
    function_definition,
    'packet.local_model_calls NOT BETWEEN 0 AND 1'
  )>0 THEN
    IF regexp_count(
      function_definition,
      'packet\.local_model_calls NOT BETWEEN 0 AND 1'
    )<>1 THEN
      RAISE EXCEPTION 'unexpected compatible local-call predicate count';
    END IF;
    function_definition := replace(
      function_definition,
      'packet.local_model_calls NOT BETWEEN 0 AND 1',
      'packet.local_model_calls<>1'
    );
    EXECUTE function_definition;
  ELSIF strpos(function_definition,'packet.local_model_calls<>1')=0 THEN
    RAISE EXCEPTION 'unexpected terminal-deferral rollback predicate';
  END IF;
END
$rollback$;

ALTER TABLE memory.v5_local_packet_disposition
  DROP CONSTRAINT v5_local_packet_disposition_local_model_calls_check;
ALTER TABLE memory.v5_local_packet_disposition
  ADD CONSTRAINT v5_local_packet_disposition_local_model_calls_check
  CHECK (local_model_calls=1) NOT VALID;
ALTER TABLE memory.v5_local_packet_disposition
  VALIDATE CONSTRAINT v5_local_packet_disposition_local_model_calls_check;

ALTER FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) OWNER TO sage;
REVOKE ALL ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) TO brains_app;

COMMIT;
