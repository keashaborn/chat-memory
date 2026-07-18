BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.entity') IS NULL
     OR to_regclass('memory.entity_alias') IS NULL
     OR to_regclass('memory.project_space') IS NULL
     OR to_regclass('memory.project_component_v5') IS NULL
     OR to_regclass('memory.project_component_alias_v5') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.normalize_entity_name_v5(text)') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL THEN
    RAISE EXCEPTION 'V5 project component entity prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.project_component_entity_binding_v5 (
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  manifest_sha256 text NOT NULL,
  prior_state_sha256 text NOT NULL,
  project_id uuid NOT NULL,
  component_id uuid NOT NULL,
  entity_id uuid NOT NULL,
  alias_set_sha256 text NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id,project_id,component_id),
  UNIQUE (owner_user_id,request_id),
  UNIQUE (owner_user_id,manifest_sha256),
  UNIQUE (owner_user_id,entity_id),
  FOREIGN KEY (owner_user_id,project_id,component_id)
    REFERENCES memory.project_component_v5(
      owner_user_id,project_id,component_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,entity_id)
    REFERENCES memory.entity(owner_user_id,entity_id) ON DELETE RESTRICT,
  CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (prior_state_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (alias_set_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (jsonb_typeof(result)='object'),
  CHECK (pg_column_size(result)<=16384),
  CHECK (invoked_by_session='brains_app'::name)
);

ALTER TABLE memory.project_component_entity_binding_v5 OWNER TO sage;
ALTER TABLE memory.project_component_entity_binding_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_component_entity_binding_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.project_component_entity_binding_v5;
CREATE POLICY owner_isolation
  ON memory.project_component_entity_binding_v5
  TO memory_v5_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS project_component_entity_binding_v5_append_only
  ON memory.project_component_entity_binding_v5;
CREATE TRIGGER project_component_entity_binding_v5_append_only
BEFORE UPDATE OR DELETE ON memory.project_component_entity_binding_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

CREATE OR REPLACE FUNCTION memory.preflight_owner_project_component_entity_v5(
  p_project_id uuid,
  p_component_id uuid
)
RETURNS TABLE(
  owner_user_id uuid,
  state text,
  existing_entity_id uuid,
  project_key text,
  component_key text,
  canonical_name text,
  aliases text[],
  prior_state_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  project_record memory.project_space%ROWTYPE;
  component_record memory.project_component_v5%ROWTYPE;
  binding_record memory.project_component_entity_binding_v5%ROWTYPE;
  entity_record memory.entity%ROWTYPE;
  alias_values text[];
  alias_state text;
  state_sha text;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_project_id IS NULL OR p_component_id IS NULL THEN
    RAISE EXCEPTION 'project_id and component_id are required'
      USING ERRCODE='22023';
  END IF;
  SELECT project.* INTO project_record
  FROM memory.project_space AS project
  WHERE project.owner_user_id=actor AND project.project_id=p_project_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project is not visible' USING ERRCODE='P0002';
  END IF;
  SELECT component.* INTO component_record
  FROM memory.project_component_v5 AS component
  WHERE component.owner_user_id=actor
    AND component.project_id=p_project_id
    AND component.component_id=p_component_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project component is not visible'
      USING ERRCODE='P0002';
  END IF;
  SELECT coalesce(array_agg(value ORDER BY value),ARRAY[]::text[])
  INTO alias_values
  FROM (
    SELECT DISTINCT memory.normalize_entity_name_v5(source_value) AS value
    FROM (
      SELECT component_record.display_name AS source_value
      UNION ALL
      SELECT alias.display_alias
      FROM memory.project_component_alias_v5 AS alias
      WHERE alias.owner_user_id=actor
        AND alias.project_id=p_project_id
        AND alias.component_id=p_component_id
    ) AS raw_aliases
  ) AS normalized
  WHERE value IS NOT NULL AND value<>'';
  IF cardinality(alias_values)<1 OR cardinality(alias_values)>20 THEN
    RAISE EXCEPTION 'component entity alias set is invalid'
      USING ERRCODE='23514';
  END IF;
  alias_state:=memory.v5_digest_text(array_to_string(alias_values,E'\n'));
  SELECT binding.* INTO binding_record
  FROM memory.project_component_entity_binding_v5 AS binding
  WHERE binding.owner_user_id=actor
    AND binding.project_id=p_project_id
    AND binding.component_id=p_component_id;
  IF FOUND THEN
    SELECT entity.* INTO entity_record
    FROM memory.entity AS entity
    WHERE entity.owner_user_id=actor
      AND entity.entity_id=binding_record.entity_id;
    IF NOT FOUND OR entity_record.status<>'active'
       OR entity_record.entity_type<>'project'
       OR binding_record.alias_set_sha256<>alias_state THEN
      RAISE EXCEPTION 'project component entity binding is inconsistent'
        USING ERRCODE='23514';
    END IF;
    state_sha:=memory.v5_digest_text(concat_ws('|',
      'memory_v1_project_component_entity_state_v5',actor::text,
      p_project_id::text,p_component_id::text,'already_exists',
      binding_record.entity_id::text,alias_state
    ));
    RETURN QUERY SELECT actor,'already_exists',binding_record.entity_id,
      project_record.project_key,component_record.component_key,
      component_record.display_name,alias_values,state_sha,
      memory.v5_digest_text(concat_ws('|',
        'memory_v1_project_component_entity_bootstrap_v5',actor::text,
        p_project_id::text,p_component_id::text,state_sha
      ));
    RETURN;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.entity AS entity
    LEFT JOIN memory.entity_alias AS alias
      ON alias.owner_user_id=entity.owner_user_id
     AND alias.entity_id=entity.entity_id
    WHERE entity.owner_user_id=actor
      AND entity.entity_type='project'
      AND entity.status='active'
      AND (
        entity.normalized_name=ANY(alias_values)
        OR alias.normalized_alias=ANY(alias_values)
      )
  ) THEN
    RAISE EXCEPTION 'unbound active project entity conflicts with component aliases'
      USING ERRCODE='23514';
  END IF;
  state_sha:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_project_component_entity_state_v5',actor::text,
    p_project_id::text,p_component_id::text,'create',
    project_record.project_key,component_record.component_key,
    component_record.display_name,alias_state
  ));
  RETURN QUERY SELECT actor,'create',NULL::uuid,
    project_record.project_key,component_record.component_key,
    component_record.display_name,alias_values,state_sha,
    memory.v5_digest_text(concat_ws('|',
      'memory_v1_project_component_entity_bootstrap_v5',actor::text,
      p_project_id::text,p_component_id::text,state_sha
    ));
END
$function$;

CREATE OR REPLACE FUNCTION memory.bootstrap_owner_project_component_entity_v5(
  p_request_id uuid,
  p_project_id uuid,
  p_component_id uuid,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  entity_id uuid,
  outcome text,
  rows_written integer,
  result jsonb
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  existing memory.project_component_entity_binding_v5%ROWTYPE;
  entity_id_value uuid;
  alias_count integer;
  alias_state text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'request ID and authorization manifest are required'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'project_component_entity',
      p_project_id::text,p_component_id::text),0
  ));
  SELECT binding.* INTO existing
  FROM memory.project_component_entity_binding_v5 AS binding
  WHERE binding.owner_user_id=actor AND binding.request_id=p_request_id;
  IF FOUND THEN
    IF existing.manifest_sha256<>p_authorization_manifest_sha256
       OR existing.project_id<>p_project_id
       OR existing.component_id<>p_component_id THEN
      RAISE EXCEPTION 'project component entity request replay mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing.entity_id,'replayed',0,existing.result;
    RETURN;
  END IF;
  SELECT * INTO preflight
  FROM memory.preflight_owner_project_component_entity_v5(
    p_project_id,p_component_id
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'project component entity authorization is stale'
      USING ERRCODE='23514';
  END IF;
  IF preflight.state='already_exists' THEN
    RETURN QUERY SELECT preflight.existing_entity_id,'already_exists',0,
      jsonb_build_object(
        'contract_version','memory_v1_project_component_entity_v5',
        'entity_id',preflight.existing_entity_id,
        'project_key',preflight.project_key,
        'component_key',preflight.component_key,
        'outcome','already_exists'
      );
    RETURN;
  END IF;
  entity_id_value:=gen_random_uuid();
  INSERT INTO memory.entity(
    entity_id,owner_user_id,entity_key,entity_type,
    canonical_name,normalized_name,metadata
  ) VALUES (
    entity_id_value,actor,
    'project_component:'||preflight.project_key||':'||preflight.component_key,
    'project',preflight.canonical_name,
    memory.normalize_entity_name_v5(preflight.canonical_name),
    jsonb_build_object(
      'contract_version','memory_v1_project_component_entity_v5',
      'identity_state','trusted_project_component',
      'project_id',p_project_id,
      'project_key',preflight.project_key,
      'component_id',p_component_id,
      'component_key',preflight.component_key,
      'created_by','memory_v5_writer'
    )
  );
  INSERT INTO memory.entity_alias(
    owner_user_id,entity_id,alias,normalized_alias,alias_type,evidence_id
  )
  SELECT actor,entity_id_value,
    min(alias.display_alias),
    memory.normalize_entity_name_v5(alias.display_alias),
    'project_component_registry_v5',NULL::uuid
  FROM memory.project_component_alias_v5 AS alias
  WHERE alias.owner_user_id=actor
    AND alias.project_id=p_project_id
    AND alias.component_id=p_component_id
  GROUP BY memory.normalize_entity_name_v5(alias.display_alias);
  GET DIAGNOSTICS alias_count=ROW_COUNT;
  IF alias_count<>cardinality(preflight.aliases) THEN
    RAISE EXCEPTION 'project component entity alias insert count mismatch'
      USING ERRCODE='23514';
  END IF;
  alias_state:=memory.v5_digest_text(array_to_string(preflight.aliases,E'\n'));
  result_value:=jsonb_build_object(
    'contract_version','memory_v1_project_component_entity_v5',
    'entity_id',entity_id_value,
    'project_id',p_project_id,
    'project_key',preflight.project_key,
    'component_id',p_component_id,
    'component_key',preflight.component_key,
    'alias_count',alias_count,
    'outcome','created'
  );
  INSERT INTO memory.project_component_entity_binding_v5(
    owner_user_id,request_id,manifest_sha256,prior_state_sha256,
    project_id,component_id,entity_id,alias_set_sha256,result,
    invoked_by_session
  ) VALUES (
    actor,p_request_id,p_authorization_manifest_sha256,
    preflight.prior_state_sha256,p_project_id,p_component_id,
    entity_id_value,alias_state,result_value,session_user
  );
  RETURN QUERY SELECT entity_id_value,'created',2+alias_count,result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.resolve_owner_project_component_entity_candidate_v5(
  p_project_key text,
  p_component_key text,
  p_normalized_name text
)
RETURNS TABLE(
  entity_id uuid,
  entity_type text,
  exact_canonical_name boolean,
  exact_alias boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF btrim(coalesce(p_project_key,''))=''
     OR btrim(coalesce(p_component_key,''))=''
     OR btrim(coalesce(p_normalized_name,''))='' THEN
    RAISE EXCEPTION 'project candidate lookup inputs are required'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT entity.entity_id,entity.entity_type,
    entity.normalized_name=p_normalized_name,
    EXISTS (
      SELECT 1 FROM memory.entity_alias AS alias
      WHERE alias.owner_user_id=entity.owner_user_id
        AND alias.entity_id=entity.entity_id
        AND alias.normalized_alias=p_normalized_name
    )
  FROM memory.project_space AS project
  JOIN memory.project_component_v5 AS component
    ON component.owner_user_id=project.owner_user_id
   AND component.project_id=project.project_id
  JOIN memory.project_component_entity_binding_v5 AS binding
    ON binding.owner_user_id=component.owner_user_id
   AND binding.project_id=component.project_id
   AND binding.component_id=component.component_id
  JOIN memory.entity AS entity
    ON entity.owner_user_id=binding.owner_user_id
   AND entity.entity_id=binding.entity_id
  WHERE project.owner_user_id=actor
    AND project.project_key=p_project_key
    AND component.component_key=p_component_key
    AND entity.status='active'
    AND (
      entity.normalized_name=p_normalized_name
      OR EXISTS (
        SELECT 1 FROM memory.entity_alias AS alias
        WHERE alias.owner_user_id=entity.owner_user_id
          AND alias.entity_id=entity.entity_id
          AND alias.normalized_alias=p_normalized_name
      )
    )
  LIMIT 2;
END
$function$;

ALTER FUNCTION memory.preflight_owner_project_component_entity_v5(uuid,uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.bootstrap_owner_project_component_entity_v5(
  uuid,uuid,uuid,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.resolve_owner_project_component_entity_candidate_v5(
  text,text,text
) OWNER TO memory_v5_writer;

REVOKE ALL ON memory.project_component_entity_binding_v5
FROM PUBLIC,brains_app;
GRANT SELECT,INSERT ON memory.project_component_entity_binding_v5
TO memory_v5_writer;
GRANT SELECT ON memory.project_space,memory.project_component_v5,
  memory.project_component_alias_v5
TO memory_v5_writer;
GRANT INSERT ON memory.entity_alias TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_owner_project_component_entity_v5(
  uuid,uuid
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.bootstrap_owner_project_component_entity_v5(
  uuid,uuid,uuid,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.resolve_owner_project_component_entity_candidate_v5(
  text,text,text
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_project_component_entity_v5(
  uuid,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.bootstrap_owner_project_component_entity_v5(
  uuid,uuid,uuid,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.resolve_owner_project_component_entity_candidate_v5(
  text,text,text
) TO brains_app;

COMMIT;
