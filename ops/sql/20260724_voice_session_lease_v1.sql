BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'voice session lease migration must run as sage, current_user=%',
      current_user;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.voice_session_lease (
  owner_user_id uuid PRIMARY KEY,
  session_id uuid NOT NULL,
  acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  renewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  CONSTRAINT voice_session_lease_expiry_check
    CHECK (expires_at > renewed_at)
);

CREATE INDEX IF NOT EXISTS voice_session_lease_expiry_idx
  ON public.voice_session_lease(expires_at);

REVOKE ALL ON public.voice_session_lease FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON public.voice_session_lease
  TO brains_app;

ALTER TABLE public.voice_session_lease ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.voice_session_lease FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS voice_session_lease_owner_policy
  ON public.voice_session_lease;
CREATE POLICY voice_session_lease_owner_policy
  ON public.voice_session_lease
  FOR ALL
  TO brains_app
  USING (
    owner_user_id::text =
      NULLIF(current_setting('app.user_id', true), '')
  )
  WITH CHECK (
    owner_user_id::text =
      NULLIF(current_setting('app.user_id', true), '')
  );

COMMENT ON TABLE public.voice_session_lease IS
  'Short authenticated owner-scoped lease enforcing one governed voice conversation across browser and installed web-app clients.';

COMMIT;
