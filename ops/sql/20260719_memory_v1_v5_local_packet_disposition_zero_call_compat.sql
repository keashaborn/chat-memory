BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $migration$
DECLARE
  constraint_definition text;
  function_definition text;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'zero-call disposition compatibility requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'zero-call disposition prerequisites are absent';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.evidence_extraction_packet_v5_local
    WHERE local_model_calls NOT BETWEEN 0 AND 1 OR external_model_calls<>0
  ) OR EXISTS (
    SELECT 1 FROM memory.v5_local_packet_disposition
    WHERE local_model_calls NOT BETWEEN 0 AND 1 OR external_model_calls<>0
  ) THEN
    RAISE EXCEPTION 'existing local-call values violate the compatibility range';
  END IF;

  SELECT pg_get_constraintdef(oid) INTO constraint_definition
  FROM pg_constraint
  WHERE conrelid='memory.v5_local_packet_disposition'::regclass
    AND conname='v5_local_packet_disposition_local_model_calls_check';
  IF constraint_definition='CHECK ((local_model_calls = 1))' THEN
    ALTER TABLE memory.v5_local_packet_disposition
      DROP CONSTRAINT v5_local_packet_disposition_local_model_calls_check;
    ALTER TABLE memory.v5_local_packet_disposition
      ADD CONSTRAINT v5_local_packet_disposition_local_model_calls_check
      CHECK (local_model_calls BETWEEN 0 AND 1) NOT VALID;
    ALTER TABLE memory.v5_local_packet_disposition
      VALIDATE CONSTRAINT v5_local_packet_disposition_local_model_calls_check;
  ELSIF constraint_definition NOT LIKE '%local_model_calls >= 0%'
        OR constraint_definition NOT LIKE '%local_model_calls <= 1%' THEN
    RAISE EXCEPTION 'unexpected local-model-call disposition constraint: %',
      constraint_definition;
  END IF;

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO function_definition;
  IF strpos(function_definition,'packet.local_model_calls<>1')>0 THEN
    IF regexp_count(function_definition,'packet\.local_model_calls<>1')<>1 THEN
      RAISE EXCEPTION 'unexpected strict local-call predicate count';
    END IF;
    function_definition := replace(
      function_definition,
      'packet.local_model_calls<>1',
      'packet.local_model_calls NOT BETWEEN 0 AND 1'
    );
    EXECUTE function_definition;
  ELSIF strpos(
    function_definition,
    'packet.local_model_calls NOT BETWEEN 0 AND 1'
  )=0 THEN
    RAISE EXCEPTION 'unexpected terminal-deferral local-call predicate';
  END IF;
END
$migration$;

ALTER FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) OWNER TO sage;
GRANT USAGE ON SCHEMA memory TO brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  TO brains_app;
REVOKE ALL ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) TO brains_app;

COMMIT;
