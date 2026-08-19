BEGIN;

DO $rollback$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'telemetry owner rollback must run as sage';
  END IF;
  IF to_regnamespace('memory') IS NULL THEN
    RAISE EXCEPTION 'memory schema is required for rollback';
  END IF;
  IF to_regprocedure('ai_operations.enforce_telemetry_retention_v1()') IS NULL THEN
    RAISE EXCEPTION 'canonical telemetry retention function is required';
  END IF;
END
$rollback$;

CREATE OR REPLACE FUNCTION memory.enforce_telemetry_retention_v1()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public,memory
AS $function$
DECLARE
  unowned_deleted bigint;
  expired_voice_deleted bigint;
  expired_general_deleted bigint;
BEGIN
  WITH deleted AS (
    DELETE FROM public.telemetry_event
    WHERE actor_user_id IS NULL
       OR actor_user_id !~*
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    RETURNING 1
  )
  SELECT count(*) INTO unowned_deleted FROM deleted;

  WITH deleted AS (
    DELETE FROM public.telemetry_event
    WHERE event_type='voice.turn.trace'
      AND occurred_at < clock_timestamp()-interval '30 days'
    RETURNING 1
  )
  SELECT count(*) INTO expired_voice_deleted FROM deleted;

  WITH deleted AS (
    DELETE FROM public.telemetry_event
    WHERE event_type<>'voice.turn.trace'
      AND occurred_at < clock_timestamp()-interval '90 days'
    RETURNING 1
  )
  SELECT count(*) INTO expired_general_deleted FROM deleted;

  RETURN jsonb_build_object(
    'contract_version','telemetry_retention_v1',
    'unowned_deleted',unowned_deleted,
    'expired_voice_deleted',expired_voice_deleted,
    'expired_general_deleted',expired_general_deleted,
    'voice_retention_days',30,
    'general_retention_days',90
  );
END
$function$;

ALTER FUNCTION memory.enforce_telemetry_retention_v1() OWNER TO sage;
REVOKE ALL ON FUNCTION memory.enforce_telemetry_retention_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.enforce_telemetry_retention_v1()
TO brains_app;
COMMENT ON FUNCTION memory.enforce_telemetry_retention_v1() IS
  'Fixed-scope telemetry retention: purge unowned rows, voice rows after 30 days, and other rows after 90 days.';

REVOKE ALL ON FUNCTION ai_operations.enforce_telemetry_retention_v1()
FROM brains_app;
DROP FUNCTION ai_operations.enforce_telemetry_retention_v1();

COMMIT;
