BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'owner self V5 migration requires sage';
  END IF;
  IF to_regclass('memory.entity') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memory_v5_writer') THEN
    RAISE EXCEPTION 'relational writer V5 prerequisites are missing';
  END IF;
END
$block$;

CREATE UNIQUE INDEX IF NOT EXISTS entity_one_active_self_v5
  ON memory.entity(owner_user_id)
  WHERE entity_type='self' AND status='active';

CREATE TABLE IF NOT EXISTS memory.owner_self_bootstrap_v5 (
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  manifest_sha256 text NOT NULL,
  prior_state_sha256 text NOT NULL,
  entity_id uuid NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, request_id),
  UNIQUE (owner_user_id, manifest_sha256),
  UNIQUE (owner_user_id, entity_id),
  FOREIGN KEY (owner_user_id, entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (prior_state_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (jsonb_typeof(result)='object'),
  CHECK (pg_column_size(result) <= 16384),
  CHECK (invoked_by_session='brains_app'::name)
);

ALTER TABLE memory.owner_self_bootstrap_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.owner_self_bootstrap_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.owner_self_bootstrap_v5;
CREATE POLICY owner_isolation ON memory.owner_self_bootstrap_v5
  TO memory_v5_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS owner_self_bootstrap_v5_append_only
  ON memory.owner_self_bootstrap_v5;
CREATE TRIGGER owner_self_bootstrap_v5_append_only
BEFORE UPDATE OR DELETE ON memory.owner_self_bootstrap_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

CREATE OR REPLACE FUNCTION memory.preflight_owner_self_v5()
RETURNS TABLE(
  owner_user_id uuid,
  state text,
  existing_entity_id uuid,
  prior_state_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  self_count integer;
  self_id uuid;
  state_sha text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT count(*)
    INTO self_count
    FROM memory.entity AS entity_row
   WHERE entity_row.owner_user_id=actor
     AND entity_row.entity_type='self'
     AND entity_row.status='active';
  SELECT entity_row.entity_id
    INTO self_id
    FROM memory.entity AS entity_row
   WHERE entity_row.owner_user_id=actor
     AND entity_row.entity_type='self'
     AND entity_row.status='active'
   ORDER BY entity_row.entity_id
   LIMIT 1;
  IF self_count > 1 THEN
    RAISE EXCEPTION 'owner has multiple active self entities'
      USING ERRCODE='23514';
  END IF;
  state_sha := memory.v5_digest_text(concat_ws('|',
    'memory_v1_owner_self_state_v5', actor::text, self_count::text,
    COALESCE(self_id::text, 'absent')
  ));
  RETURN QUERY SELECT
    actor,
    CASE WHEN self_count=0 THEN 'create' ELSE 'already_exists' END,
    self_id,
    state_sha,
    memory.v5_digest_text(concat_ws('|',
      'memory_v1_owner_self_bootstrap_v5', actor::text, state_sha
    ));
END
$function$;

CREATE OR REPLACE FUNCTION memory.bootstrap_owner_self_v5(
  p_request_id uuid,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  entity_id uuid,
  outcome text,
  rows_written integer,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  existing memory.owner_self_bootstrap_v5%ROWTYPE;
  created_entity_id uuid;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'request ID and authorization manifest are required'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text, 1780127315)
  );

  SELECT * INTO existing
    FROM memory.owner_self_bootstrap_v5 AS stored
   WHERE stored.owner_user_id=actor AND stored.request_id=p_request_id;
  IF FOUND THEN
    IF existing.manifest_sha256 <> p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'owner self request replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing.entity_id, 'replayed', 0, existing.result;
    RETURN;
  END IF;

  SELECT * INTO existing
    FROM memory.owner_self_bootstrap_v5 AS stored
   WHERE stored.owner_user_id=actor
     AND stored.manifest_sha256=p_authorization_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT existing.entity_id, 'replayed', 0, existing.result;
    RETURN;
  END IF;

  SELECT * INTO preflight FROM memory.preflight_owner_self_v5();
  IF preflight.authorization_manifest_sha256
       <> p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'owner self authorization manifest is stale'
      USING ERRCODE='23514';
  END IF;

  IF preflight.state='already_exists' THEN
    result_value := jsonb_build_object(
      'contract_version', 'memory_v1_owner_self_bootstrap_v5',
      'owner_user_id', actor,
      'entity_id', preflight.existing_entity_id,
      'outcome', 'already_exists'
    );
    RETURN QUERY SELECT
      preflight.existing_entity_id, 'already_exists', 0, result_value;
    RETURN;
  END IF;

  created_entity_id := gen_random_uuid();
  INSERT INTO memory.entity(
    entity_id, owner_user_id, entity_key, entity_type,
    canonical_name, normalized_name, metadata
  ) VALUES (
    created_entity_id, actor, gen_random_uuid()::text, 'self',
    'Self', 'self', jsonb_build_object(
      'contract_version', 'memory_v1_owner_self_entity_v5',
      'identity_state', 'trusted_owner_self',
      'created_by', 'memory_v5_writer'
    )
  );
  result_value := jsonb_build_object(
    'contract_version', 'memory_v1_owner_self_bootstrap_v5',
    'owner_user_id', actor,
    'entity_id', created_entity_id,
    'outcome', 'created'
  );
  INSERT INTO memory.owner_self_bootstrap_v5(
    owner_user_id, request_id, manifest_sha256, prior_state_sha256,
    entity_id, result, invoked_by_session
  ) VALUES (
    actor, p_request_id, p_authorization_manifest_sha256,
    preflight.prior_state_sha256, created_entity_id,
    result_value, session_user
  );
  RETURN QUERY SELECT created_entity_id, 'created', 2, result_value;
END
$function$;

ALTER TABLE memory.owner_self_bootstrap_v5 OWNER TO sage;
ALTER FUNCTION memory.preflight_owner_self_v5() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.bootstrap_owner_self_v5(uuid,text) OWNER TO memory_v5_writer;

REVOKE ALL ON memory.owner_self_bootstrap_v5 FROM PUBLIC, brains_app;
GRANT SELECT, INSERT ON memory.owner_self_bootstrap_v5 TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_owner_self_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.bootstrap_owner_self_v5(uuid,text)
  FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_self_v5() TO brains_app;
GRANT EXECUTE ON FUNCTION memory.bootstrap_owner_self_v5(uuid,text) TO brains_app;

COMMIT;
