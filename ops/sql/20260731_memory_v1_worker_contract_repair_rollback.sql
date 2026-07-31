BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'worker contract repair rollback requires sage';
  END IF;
  IF to_regclass(
       'memory.v5_local_claim_projection_terminal_v1'
     ) IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.v5_local_claim_projection_terminal_v1
     ) THEN
    RAISE EXCEPTION
      'append-only terminal history exists; use code rollback and retain schema';
  END IF;
END
$preflight$;

DO $packet_exact$
DECLARE
  definition text;
BEGIN
  definition:=pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  );
  IF position('AND packet.local_model_calls BETWEEN 0 AND 1' IN definition)>0
  THEN
    EXECUTE replace(
      definition,
      'AND packet.local_model_calls BETWEEN 0 AND 1',
      'AND packet.local_model_calls=1'
    );
  END IF;
END
$packet_exact$;

DO $claim_planner$
DECLARE
  definition text;
  patched text;
  original text;
BEGIN
  definition:=pg_get_functiondef(
    'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
  );
  patched:=$patched$
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_claim_projection_terminal_v1 AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
$patched$;
  original:=$original$
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
$original$;
  IF position(patched IN definition)>0 THEN
    EXECUTE replace(definition,patched,original);
  END IF;
END
$claim_planner$;

DROP FUNCTION IF EXISTS
  memory.register_owner_v5_local_claim_projection_v2(
    uuid,uuid,uuid,uuid,text,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.local_claim_projection_source_accepted_v1(uuid,uuid);
DROP FUNCTION IF EXISTS
  memory.inspect_existing_claim_projection_v1(uuid,text);
DROP TABLE IF EXISTS memory.v5_local_claim_projection_terminal_v1;
DROP POLICY IF EXISTS local_writer_read
  ON memory.v5_local_claim_projection_admission;
REVOKE SELECT ON memory.v5_local_claim_projection_admission
  FROM memory_v5_writer;
REVOKE EXECUTE ON FUNCTION
  memory.register_owner_v5_local_claim_projection_v1(
    uuid,uuid,uuid,uuid,text,text,text,text
  ) FROM memory_v5_writer;

COMMIT;
