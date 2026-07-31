BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname='lifeswitch_chat_account_writer_v1'
  ) THEN
    CREATE ROLE lifeswitch_chat_account_writer_v1
      NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
$$;

REVOKE ALL ON SCHEMA lifeswitch_chat FROM lifeswitch_chat_account_writer_v1;
GRANT USAGE ON SCHEMA lifeswitch_chat TO lifeswitch_chat_account_writer_v1;

ALTER TABLE lifeswitch_chat.account_timezone_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY account_timezone_owner_write_v1
  ON lifeswitch_chat.account_timezone_v1
  FOR ALL TO sage
  USING (
    owner_user_id=NULLIF(
      pg_catalog.current_setting('app.user_id',true),''
    )::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(
      pg_catalog.current_setting('app.user_id',true),''
    )::uuid
  );

CREATE TABLE lifeswitch_chat.account_timezone_history_v1 (
  event_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  previous_timezone_name text,
  timezone_name text NOT NULL,
  previous_revision bigint NOT NULL CHECK (previous_revision >= 0),
  revision bigint NOT NULL CHECK (revision >= 1),
  request_id_sha256 text NOT NULL
    CHECK (request_id_sha256 ~ '^[0-9a-f]{64}$'),
  changed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CHECK (previous_revision < revision),
  CHECK (pg_catalog.char_length(timezone_name) BETWEEN 1 AND 80),
  CHECK (
    previous_timezone_name IS NULL
    OR pg_catalog.char_length(previous_timezone_name) BETWEEN 1 AND 80
  )
);
ALTER TABLE lifeswitch_chat.account_timezone_history_v1 OWNER TO sage;
ALTER TABLE lifeswitch_chat.account_timezone_history_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_chat.account_timezone_history_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY account_timezone_history_owner_write_v1
  ON lifeswitch_chat.account_timezone_history_v1
  FOR ALL TO sage
  USING (
    owner_user_id=NULLIF(
      pg_catalog.current_setting('app.user_id',true),''
    )::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(
      pg_catalog.current_setting('app.user_id',true),''
    )::uuid
  );
REVOKE ALL ON lifeswitch_chat.account_timezone_history_v1
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1,
    lifeswitch_chat_binding_writer_v1,lifeswitch_chat_account_writer_v1;

CREATE FUNCTION lifeswitch_chat.read_account_timezone_setting_v1(
  p_owner_user_id uuid
)
RETURNS TABLE(
  timezone_name text,
  timezone_source text,
  revision bigint,
  updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_actor uuid;
  v_owner uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted backend session required' USING ERRCODE='42501';
  END IF;
  v_actor := NULLIF(pg_catalog.current_setting('app.user_id',true),'')::uuid;
  v_owner := NULLIF(
    pg_catalog.current_setting('app.lifeswitch_owner_id',true),''
  )::uuid;
  IF v_actor IS NULL OR v_actor <> p_owner_user_id OR v_owner <> p_owner_user_id THEN
    RAISE EXCEPTION 'authenticated owner mismatch' USING ERRCODE='42501';
  END IF;
  RETURN QUERY
  SELECT value.timezone_name,value.source,value.revision,value.updated_at
  FROM lifeswitch_chat.account_timezone_v1 AS value
  WHERE value.owner_user_id=p_owner_user_id;
END
$$;
ALTER FUNCTION lifeswitch_chat.read_account_timezone_setting_v1(uuid)
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.read_account_timezone_setting_v1(uuid)
  FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1,
    lifeswitch_chat_binding_writer_v1;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.read_account_timezone_setting_v1(uuid)
  TO lifeswitch_chat_account_writer_v1;

CREATE FUNCTION lifeswitch_chat.write_account_timezone_setting_v1(
  p_owner_user_id uuid,
  p_timezone_name text,
  p_expected_revision bigint,
  p_request_id_sha256 text
)
RETURNS TABLE(
  timezone_name text,
  timezone_source text,
  revision bigint,
  updated_at timestamptz,
  changed boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $$
DECLARE
  v_actor uuid;
  v_owner uuid;
  v_previous_timezone text;
  v_previous_source text;
  v_previous_revision bigint;
  v_new_revision bigint;
  v_updated_at timestamptz;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'trusted backend session required' USING ERRCODE='42501';
  END IF;
  v_actor := NULLIF(pg_catalog.current_setting('app.user_id',true),'')::uuid;
  v_owner := NULLIF(
    pg_catalog.current_setting('app.lifeswitch_owner_id',true),''
  )::uuid;
  IF v_actor IS NULL OR v_actor <> p_owner_user_id OR v_owner <> p_owner_user_id THEN
    RAISE EXCEPTION 'authenticated owner mismatch' USING ERRCODE='42501';
  END IF;
  IF p_expected_revision < 0 THEN
    RAISE EXCEPTION 'invalid expected revision' USING ERRCODE='22023';
  END IF;
  IF p_request_id_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid request binding' USING ERRCODE='22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_timezone_names
    WHERE name=p_timezone_name
  ) THEN
    RAISE EXCEPTION 'invalid timezone' USING ERRCODE='22023';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_owner_user_id::text,0)
  );

  SELECT value.timezone_name,value.source,value.revision
  INTO v_previous_timezone,v_previous_source,v_previous_revision
  FROM lifeswitch_chat.account_timezone_v1 AS value
  WHERE value.owner_user_id=p_owner_user_id
  FOR UPDATE;

  IF NOT FOUND THEN
    IF p_expected_revision <> 0 THEN
      RAISE EXCEPTION 'timezone revision conflict' USING ERRCODE='40001';
    END IF;
    v_previous_revision := 0;
  ELSIF v_previous_revision <> p_expected_revision THEN
    RAISE EXCEPTION 'timezone revision conflict' USING ERRCODE='40001';
  ELSIF v_previous_timezone=p_timezone_name
        AND v_previous_source='account_setting' THEN
    RETURN QUERY SELECT
      v_previous_timezone,'account_setting'::text,
      v_previous_revision,
      (SELECT value.updated_at
       FROM lifeswitch_chat.account_timezone_v1 AS value
       WHERE value.owner_user_id=p_owner_user_id),
      false;
    RETURN;
  END IF;

  v_new_revision := v_previous_revision + 1;
  v_updated_at := pg_catalog.clock_timestamp();
  INSERT INTO lifeswitch_chat.account_timezone_v1(
    owner_user_id,timezone_name,source,revision,created_at,updated_at
  ) VALUES (
    p_owner_user_id,p_timezone_name,'account_setting',v_new_revision,
    v_updated_at,v_updated_at
  )
  ON CONFLICT (owner_user_id) DO UPDATE
  SET timezone_name=EXCLUDED.timezone_name,
      source='account_setting',
      revision=EXCLUDED.revision,
      updated_at=EXCLUDED.updated_at;

  INSERT INTO lifeswitch_chat.account_timezone_history_v1(
    owner_user_id,previous_timezone_name,timezone_name,
    previous_revision,revision,request_id_sha256,changed_at
  ) VALUES (
    p_owner_user_id,v_previous_timezone,p_timezone_name,
    v_previous_revision,v_new_revision,p_request_id_sha256,v_updated_at
  );

  RETURN QUERY SELECT
    p_timezone_name,'account_setting'::text,v_new_revision,v_updated_at,true;
END
$$;
ALTER FUNCTION lifeswitch_chat.write_account_timezone_setting_v1(
  uuid,text,bigint,text
) OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.write_account_timezone_setting_v1(
  uuid,text,bigint,text
) FROM PUBLIC,brains_app,lifeswitch_chat_reader_v1,
  lifeswitch_chat_binding_writer_v1;
GRANT EXECUTE ON FUNCTION lifeswitch_chat.write_account_timezone_setting_v1(
  uuid,text,bigint,text
) TO lifeswitch_chat_account_writer_v1;

CREATE FUNCTION lifeswitch_chat.reject_account_timezone_history_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path=''
AS $$
BEGIN
  RAISE EXCEPTION 'account timezone history is append-only';
END
$$;
ALTER FUNCTION lifeswitch_chat.reject_account_timezone_history_mutation_v1()
  OWNER TO sage;
REVOKE ALL ON FUNCTION lifeswitch_chat.reject_account_timezone_history_mutation_v1()
  FROM PUBLIC;
CREATE TRIGGER account_timezone_history_immutable_v1
BEFORE UPDATE OR DELETE
ON lifeswitch_chat.account_timezone_history_v1
FOR EACH ROW EXECUTE FUNCTION
  lifeswitch_chat.reject_account_timezone_history_mutation_v1();

GRANT lifeswitch_chat_account_writer_v1 TO brains_app
  WITH INHERIT FALSE,SET TRUE;

COMMENT ON TABLE lifeswitch_chat.account_timezone_history_v1 IS
  'Append-only owner-scoped audit history for explicit account timezone changes.';
COMMENT ON FUNCTION lifeswitch_chat.write_account_timezone_setting_v1(
  uuid,text,bigint,text
) IS 'Owner-bound optimistic account timezone update through the trusted backend only.';

COMMIT;
