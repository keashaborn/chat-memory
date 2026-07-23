BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'telemetry governance migration must run as sage';
  END IF;
  IF to_regclass('public.telemetry_event') IS NULL THEN
    RAISE EXCEPTION 'public.telemetry_event is required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION memory.enforce_telemetry_retention_v1()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public,memory
AS $$
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
$$;

ALTER FUNCTION memory.enforce_telemetry_retention_v1() OWNER TO sage;
REVOKE ALL ON FUNCTION memory.enforce_telemetry_retention_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.enforce_telemetry_retention_v1()
TO brains_app;

SELECT memory.enforce_telemetry_retention_v1();

ALTER TABLE public.telemetry_event
  ALTER COLUMN actor_user_id SET NOT NULL;

ALTER TABLE public.telemetry_event
  DROP CONSTRAINT IF EXISTS telemetry_event_actor_user_id_uuid_check;
ALTER TABLE public.telemetry_event
  ADD CONSTRAINT telemetry_event_actor_user_id_uuid_check
  CHECK (
    actor_user_id ~*
      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  );

REVOKE ALL ON public.telemetry_event FROM PUBLIC;

ALTER TABLE public.telemetry_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telemetry_event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS telemetry_owner_policy
ON public.telemetry_event;
CREATE POLICY telemetry_owner_policy
ON public.telemetry_event
FOR ALL
TO brains_app
USING (
  actor_user_id =
    NULLIF(current_setting('app.user_id',true),'')
)
WITH CHECK (
  actor_user_id =
    NULLIF(current_setting('app.user_id',true),'')
);

COMMENT ON TABLE public.telemetry_event IS
  'Owner-scoped operational telemetry. Voice events retain 30 days; other authenticated diagnostics retain 90 days.';
COMMENT ON FUNCTION memory.enforce_telemetry_retention_v1() IS
  'Fixed-scope telemetry retention: purge unowned rows, voice rows after 30 days, and other rows after 90 days.';

COMMIT;
