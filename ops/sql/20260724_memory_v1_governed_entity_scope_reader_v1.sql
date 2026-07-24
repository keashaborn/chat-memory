BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'governed entity-scope reader migration requires sage';
  END IF;
  IF to_regprocedure('memory.require_v5_reader_context()') IS NULL
     OR to_regclass('memory.entity') IS NULL
     OR to_regclass('memory.entity_alias') IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.projection_apply_event') IS NULL THEN
    RAISE EXCEPTION 'governed entity-scope reader prerequisites are missing';
  END IF;
END
$prerequisite$;

GRANT SELECT ON
  memory.entity,
  memory.entity_alias,
  memory.claim,
  memory.projection_apply_event
TO memory_v5_reader;

CREATE OR REPLACE FUNCTION memory.read_governed_entity_scope_entities_v1()
RETURNS TABLE(
  owner_user_id uuid,
  entity_id uuid,
  entity_type text,
  canonical_name text,
  normalized_name text,
  identity_state text,
  relationship_role text,
  normalized_aliases text[]
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  row_count integer;
BEGIN
  actor := memory.require_v5_reader_context();

  SELECT count(*)
    INTO row_count
    FROM memory.entity AS entity
   WHERE entity.owner_user_id=actor
     AND entity.status='active';
  IF row_count > 1000 THEN
    RAISE EXCEPTION 'entity scope exceeds 1000 active entities'
      USING ERRCODE='54000';
  END IF;

  RETURN QUERY
  SELECT
    entity.owner_user_id,
    entity.entity_id,
    entity.entity_type,
    entity.canonical_name,
    entity.normalized_name,
    NULLIF(entity.metadata->>'identity_state',''),
    NULLIF(entity.metadata->>'relationship_role',''),
    COALESCE(
      array_agg(
        DISTINCT alias.normalized_alias
        ORDER BY alias.normalized_alias
      ) FILTER (WHERE alias.normalized_alias IS NOT NULL),
      ARRAY[]::text[]
    )
  FROM memory.entity AS entity
  LEFT JOIN memory.entity_alias AS alias
    ON alias.owner_user_id=entity.owner_user_id
   AND alias.entity_id=entity.entity_id
  WHERE entity.owner_user_id=actor
    AND entity.status='active'
  GROUP BY
    entity.owner_user_id,
    entity.entity_id,
    entity.entity_type,
    entity.canonical_name,
    entity.normalized_name,
    entity.metadata
  ORDER BY entity.entity_id;
END
$function$;

CREATE OR REPLACE FUNCTION memory.read_governed_entity_scope_edges_v1()
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  predicate text,
  subject_entity_id uuid,
  subject_entity_type text,
  object_entity_id uuid,
  object_entity_type text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  row_count integer;
BEGIN
  actor := memory.require_v5_reader_context();

  SELECT count(*)
    INTO row_count
    FROM memory.claim AS claim
   WHERE claim.owner_user_id=actor
     AND claim.status='supported'
     AND claim.object_entity_id IS NOT NULL
     AND claim.canonical_key LIKE 'v5:%'
     AND claim.metadata->>'memory_contract'='memory_projection_v5'
     AND EXISTS (
       SELECT 1
         FROM memory.projection_apply_event AS event
        WHERE event.owner_user_id=claim.owner_user_id
          AND event.resulting_claim_id=claim.claim_id
          AND event.lane='claim'
          AND event.outcome='applied'
     );
  IF row_count > 2000 THEN
    RAISE EXCEPTION 'entity scope exceeds 2000 governed edges'
      USING ERRCODE='54000';
  END IF;

  RETURN QUERY
  SELECT
    claim.owner_user_id,
    claim.claim_id,
    claim.predicate,
    claim.subject_entity_id,
    subject.entity_type,
    claim.object_entity_id,
    object_entity.entity_type
  FROM memory.claim AS claim
  JOIN memory.entity AS subject
    ON subject.owner_user_id=claim.owner_user_id
   AND subject.entity_id=claim.subject_entity_id
   AND subject.status='active'
  JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=claim.owner_user_id
   AND object_entity.entity_id=claim.object_entity_id
   AND object_entity.status='active'
  WHERE claim.owner_user_id=actor
    AND claim.status='supported'
    AND claim.object_entity_id IS NOT NULL
    AND claim.canonical_key LIKE 'v5:%'
    AND claim.metadata->>'memory_contract'='memory_projection_v5'
    AND EXISTS (
      SELECT 1
        FROM memory.projection_apply_event AS event
       WHERE event.owner_user_id=claim.owner_user_id
         AND event.resulting_claim_id=claim.claim_id
         AND event.lane='claim'
         AND event.outcome='applied'
    )
  ORDER BY
    claim.predicate,
    claim.subject_entity_id,
    claim.object_entity_id,
    claim.claim_id;
END
$function$;

ALTER FUNCTION memory.read_governed_entity_scope_entities_v1()
  OWNER TO memory_v5_reader;
ALTER FUNCTION memory.read_governed_entity_scope_edges_v1()
  OWNER TO memory_v5_reader;

REVOKE ALL ON FUNCTION memory.read_governed_entity_scope_entities_v1()
  FROM PUBLIC,memory_v5_writer;
REVOKE ALL ON FUNCTION memory.read_governed_entity_scope_edges_v1()
  FROM PUBLIC,memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.read_governed_entity_scope_entities_v1()
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.read_governed_entity_scope_edges_v1()
  TO brains_app;

COMMIT;
