BEGIN;

DROP POLICY IF EXISTS telemetry_owner_policy
ON public.telemetry_event;
ALTER TABLE public.telemetry_event NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.telemetry_event DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.telemetry_event
  DROP CONSTRAINT IF EXISTS telemetry_event_actor_user_id_uuid_check;
ALTER TABLE public.telemetry_event
  ALTER COLUMN actor_user_id DROP NOT NULL;

REVOKE ALL ON FUNCTION memory.enforce_telemetry_retention_v1()
FROM brains_app;
DROP FUNCTION IF EXISTS memory.enforce_telemetry_retention_v1();

COMMIT;
