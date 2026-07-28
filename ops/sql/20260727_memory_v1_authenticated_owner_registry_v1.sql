BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'authenticated owner registry migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'authenticated owner registry prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_authenticated_owner_registry_maintainer') IS NULL THEN
    CREATE ROLE memory_authenticated_owner_registry_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_authenticated_owner_registry_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.authenticated_owner_registry_v1 (
  owner_user_id uuid PRIMARY KEY,
  first_verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  last_verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  auth_contract_version text NOT NULL,
  last_request_id_sha256 text NOT NULL,
  CHECK (last_verified_at >= first_verified_at),
  CHECK (auth_contract_version ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'),
  CHECK (last_request_id_sha256 ~ '^[0-9a-f]{64}$')
);

INSERT INTO memory.authenticated_owner_registry_v1(
  owner_user_id,
  auth_contract_version,
  last_request_id_sha256
)
SELECT
  seed.owner_user_id,
  'current_account_bootstrap_20260727',
  encode(
    public.digest(
      'current-account-bootstrap|' || seed.owner_user_id::text,
      'sha256'
    ),
    'hex'
  )
FROM (
  VALUES
    ('1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid),
    ('557ea042-cb82-48f8-9429-472e96c957ef'::uuid),
    ('5c9f624a-a66d-4183-babb-b3a0f0f4e733'::uuid),
    ('673d64a3-c4ba-4d1c-89e3-e0c579022fad'::uuid),
    ('818b60b9-89bd-442a-998c-fc1924184dfc'::uuid),
    ('d839b4bc-0bd2-4f2d-aafe-0f3f75883db8'::uuid)
) AS seed(owner_user_id)
ON CONFLICT (owner_user_id) DO NOTHING;

REVOKE ALL ON TABLE memory.authenticated_owner_registry_v1
  FROM PUBLIC,brains_app;
ALTER TABLE memory.authenticated_owner_registry_v1
  OWNER TO memory_authenticated_owner_registry_maintainer;

GRANT USAGE ON SCHEMA memory
  TO memory_authenticated_owner_registry_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_authenticated_owner_registry_maintainer;

CREATE OR REPLACE FUNCTION memory.register_authenticated_owner_v1(
  p_owner_user_id uuid,
  p_auth_contract_version text,
  p_request_id text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  request_sha256 text;
  inserted boolean;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'memory_authenticated_owner_registry_maintainer' THEN
    RAISE EXCEPTION 'authenticated owner registration requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR actor <> p_owner_user_id THEN
    RAISE EXCEPTION 'authenticated owner registration actor mismatch'
      USING ERRCODE='42501';
  END IF;
  IF p_auth_contract_version IS NULL
     OR p_auth_contract_version !~ '^[a-z0-9][a-z0-9_.-]{2,63}$'
     OR p_request_id IS NULL
     OR length(p_request_id) NOT BETWEEN 1 AND 128 THEN
    RAISE EXCEPTION 'authenticated owner registration input is invalid'
      USING ERRCODE='22023';
  END IF;

  request_sha256 := encode(public.digest(p_request_id, 'sha256'), 'hex');
  INSERT INTO memory.authenticated_owner_registry_v1(
    owner_user_id,
    auth_contract_version,
    last_request_id_sha256
  )
  VALUES (
    p_owner_user_id,
    p_auth_contract_version,
    request_sha256
  )
  ON CONFLICT (owner_user_id) DO UPDATE
  SET last_verified_at=clock_timestamp(),
      auth_contract_version=EXCLUDED.auth_contract_version,
      last_request_id_sha256=EXCLUDED.last_request_id_sha256
  RETURNING (xmax=0) INTO inserted;

  RETURN CASE WHEN inserted THEN 'registered' ELSE 'refreshed' END;
END
$function$;

ALTER FUNCTION memory.register_authenticated_owner_v1(uuid,text,text)
  OWNER TO memory_authenticated_owner_registry_maintainer;
REVOKE ALL ON FUNCTION memory.register_authenticated_owner_v1(uuid,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.register_authenticated_owner_v1(uuid,text,text)
  TO brains_app;

CREATE OR REPLACE FUNCTION memory.list_recent_authenticated_owners_v1(
  p_max_age interval DEFAULT interval '90 days',
  p_limit integer DEFAULT 1000
)
RETURNS TABLE(owner_user_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'memory_authenticated_owner_registry_maintainer' THEN
    RAISE EXCEPTION 'authenticated owner listing requires brains_app'
      USING ERRCODE='42501';
  END IF;
  IF p_max_age < interval '1 day'
     OR p_max_age > interval '365 days'
     OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'authenticated owner listing input is invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT registry.owner_user_id
  FROM memory.authenticated_owner_registry_v1 AS registry
  WHERE registry.last_verified_at >= clock_timestamp() - p_max_age
  ORDER BY registry.owner_user_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.list_recent_authenticated_owners_v1(interval,integer)
  OWNER TO memory_authenticated_owner_registry_maintainer;
REVOKE ALL ON FUNCTION
  memory.list_recent_authenticated_owners_v1(interval,integer)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.list_recent_authenticated_owners_v1(interval,integer)
  TO brains_app;

COMMIT;
