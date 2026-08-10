-- Content-free monotonic pilot marker for the clean governed Memory successor.
-- The migration runner supplies BEGIN/COMMIT, timeouts, and an advisory lock.

DO $preflight$
BEGIN
  IF pg_catalog.current_database() <> 'governed_memory'
     OR current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'pilot marker migration requires governed_memory_owner in governed_memory';
  END IF;
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'pilot marker migration requires read committed';
  END IF;
END;
$preflight$;

CREATE FUNCTION memory_private.pilot_marker_receipt_sha256(
  p_pilot_id text,
  p_operation_id uuid,
  p_pilot_contract_sha256 text,
  p_authorization_receipt_sha256 text,
  p_started_at timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.pilot_ever_started.v1' || E'\n'
      || memory_private.framed_utf8_field('pilot_id', p_pilot_id)
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'pilot_contract_sha256', p_pilot_contract_sha256
         )
      || memory_private.framed_utf8_field(
           'authorization_receipt_sha256',
           p_authorization_receipt_sha256
         )
      || memory_private.framed_utf8_field(
           'started_at', memory_private.timestamp_utc_text(p_started_at)
         ),
    'UTF8'
  )), 'hex')
$function$;

CREATE TABLE memory.pilot_marker (
  pilot_ever_started boolean,
  pilot_id text NOT NULL,
  operation_id uuid NOT NULL UNIQUE,
  pilot_contract_sha256 text NOT NULL,
  authorization_receipt_sha256 text NOT NULL,
  started_at timestamptz NOT NULL,
  marker_receipt_sha256 text NOT NULL UNIQUE,
  CONSTRAINT pilot_marker_singleton PRIMARY KEY (pilot_ever_started),
  CONSTRAINT pilot_marker_true_only CHECK (pilot_ever_started),
  CONSTRAINT pilot_marker_id CHECK (
    pilot_id ~ '^[a-z][a-z0-9_.:-]{0,127}$'
  ),
  CONSTRAINT pilot_marker_hashes CHECK (
    pilot_contract_sha256 ~ '^[0-9a-f]{64}$'
    AND authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'
    AND marker_receipt_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT pilot_marker_receipt_binding CHECK (
    marker_receipt_sha256 = memory_private.pilot_marker_receipt_sha256(
      pilot_id,
      operation_id,
      pilot_contract_sha256,
      authorization_receipt_sha256,
      started_at
    )
  )
);

ALTER TABLE memory.pilot_marker OWNER TO governed_memory_owner;
REVOKE ALL ON TABLE memory.pilot_marker FROM PUBLIC;
REVOKE ALL ON TABLE memory.pilot_marker
  FROM governed_memory_api, governed_memory_worker;
ALTER TABLE memory.pilot_marker ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.pilot_marker FORCE ROW LEVEL SECURITY;
CREATE POLICY pilot_marker_owner_only ON memory.pilot_marker
  FOR ALL TO governed_memory_owner
  USING (true)
  WITH CHECK (true);

CREATE FUNCTION memory_private.guard_pilot_marker_append_only()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'pilot marker is append-only and monotonic'
    USING ERRCODE = '55000';
END;
$function$;

CREATE TRIGGER pilot_marker_append_only
  BEFORE UPDATE OR DELETE ON memory.pilot_marker
  FOR EACH ROW EXECUTE FUNCTION
    memory_private.guard_pilot_marker_append_only();

CREATE FUNCTION memory_private.mark_pilot_started(
  p_pilot_id text,
  p_operation_id uuid,
  p_pilot_contract_sha256 text,
  p_authorization_receipt_sha256 text,
  p_started_at timestamptz
)
RETURNS TABLE (
  pilot_ever_started boolean,
  marker_receipt_sha256 text,
  replayed boolean
)
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  expected_receipt text;
  existing memory.pilot_marker%ROWTYPE;
  inserted_count integer;
BEGIN
  IF p_pilot_id !~ '^[a-z][a-z0-9_.:-]{0,127}$'
     OR p_pilot_contract_sha256 !~ '^[0-9a-f]{64}$'
     OR p_authorization_receipt_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid pilot marker command' USING ERRCODE = '22023';
  END IF;
  expected_receipt := memory_private.pilot_marker_receipt_sha256(
    p_pilot_id,
    p_operation_id,
    p_pilot_contract_sha256,
    p_authorization_receipt_sha256,
    p_started_at
  );
  INSERT INTO memory.pilot_marker (
    pilot_ever_started,
    pilot_id,
    operation_id,
    pilot_contract_sha256,
    authorization_receipt_sha256,
    started_at,
    marker_receipt_sha256
  ) VALUES (
    true,
    p_pilot_id,
    p_operation_id,
    p_pilot_contract_sha256,
    p_authorization_receipt_sha256,
    p_started_at,
    expected_receipt
  )
  ON CONFLICT ON CONSTRAINT pilot_marker_singleton DO NOTHING;
  GET DIAGNOSTICS inserted_count = ROW_COUNT;

  SELECT value.* INTO STRICT existing
  FROM memory.pilot_marker AS value
  WHERE value.pilot_ever_started;
  IF existing.marker_receipt_sha256 <> expected_receipt
     OR existing.pilot_id <> p_pilot_id
     OR existing.operation_id <> p_operation_id
     OR existing.pilot_contract_sha256 <> p_pilot_contract_sha256
     OR existing.authorization_receipt_sha256
          <> p_authorization_receipt_sha256
     OR existing.started_at <> p_started_at THEN
    RAISE EXCEPTION 'pilot marker conflicting replay' USING ERRCODE = '23505';
  END IF;
  RETURN QUERY SELECT true, expected_receipt, inserted_count = 0;
END;
$function$;

CREATE FUNCTION memory_private.read_pilot_marker()
RETURNS TABLE (
  pilot_ever_started boolean,
  pilot_id text,
  operation_id uuid,
  pilot_contract_sha256 text,
  authorization_receipt_sha256 text,
  started_at timestamptz,
  marker_receipt_sha256 text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
  SELECT true, value.pilot_id, value.operation_id,
         value.pilot_contract_sha256,
         value.authorization_receipt_sha256,
         value.started_at,
         value.marker_receipt_sha256
  FROM memory.pilot_marker AS value
  WHERE value.pilot_ever_started
$function$;

ALTER FUNCTION memory_private.pilot_marker_receipt_sha256(
  text,uuid,text,text,timestamptz
) OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_pilot_marker_append_only()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.mark_pilot_started(
  text,uuid,text,text,timestamptz
) OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.read_pilot_marker()
  OWNER TO governed_memory_owner;

REVOKE ALL ON FUNCTION memory_private.pilot_marker_receipt_sha256(
  text,uuid,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_pilot_marker_append_only()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.mark_pilot_started(
  text,uuid,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.read_pilot_marker() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory_private.read_pilot_marker()
  TO governed_memory_worker;
