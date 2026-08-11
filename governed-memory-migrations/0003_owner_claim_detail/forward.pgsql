-- Owner-scoped claim fact detail for governed_memory_owner_claim_detail_0003.
-- Apply only to the isolated governed_memory database after foundation 0001.
-- The migration runner supplies the transaction, timeouts, and advisory lock.

DO $preflight$
BEGIN
  IF pg_catalog.current_database() <> 'governed_memory'
     OR current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'claim detail migration requires governed_memory_owner in governed_memory';
  END IF;
  IF pg_catalog.to_regnamespace('memory_private') IS NULL
     OR pg_catalog.to_regclass('memory.claim') IS NULL
     OR pg_catalog.to_regclass('memory.claim_revision') IS NULL
     OR pg_catalog.to_regprocedure(
          'memory_private.current_owner_id()'
        ) IS NULL THEN
    RAISE EXCEPTION 'governed memory foundation is absent';
  END IF;
  IF pg_catalog.to_regprocedure(
       'memory_private.read_claim(uuid)'
     ) IS NOT NULL THEN
    RAISE EXCEPTION 'claim detail function already exists';
  END IF;
END;
$preflight$;

CREATE FUNCTION memory_private.read_claim(p_claim_id uuid)
RETURNS TABLE(
  claim_id uuid,
  lifecycle_state text,
  revision_id uuid,
  revision_number integer,
  revision_sha256 text,
  current_state_sha256 text,
  revision_fact_policy_sha256 text,
  predicate_catalog_sha256 text,
  selected_sha256 text,
  selection_binding_sha256 text,
  object_kind text,
  predicate text,
  epistemic_state text,
  sensitivity text,
  updated_at timestamptz,
  subject_entity_type text,
  subject_entity_key text,
  subject_display_name text,
  object_entity_type text,
  object_entity_key text,
  object_display_name text,
  object_literal text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_claim_id IS NULL THEN
    RAISE EXCEPTION 'invalid claim-detail input' USING ERRCODE = '22023';
  END IF;
  IF memory_private.owner_source_erasure_active(actor) THEN
    RETURN;
  END IF;
  RETURN QUERY
  SELECT claim.claim_id, claim.lifecycle_state, revision.revision_id,
         revision.revision_number, revision.revision_sha256,
         claim.current_state_sha256, revision.fact_policy_sha256,
         revision.predicate_catalog_sha256, revision.selected_sha256,
         revision.selection_binding_sha256, revision.object_kind,
         revision.predicate, revision.epistemic_state,
         revision.sensitivity, claim.updated_at,
         revision.subject_entity_type, revision.subject_entity_key,
         revision.subject_display_name, revision.object_entity_type,
         revision.object_entity_key, revision.object_display_name,
         CASE WHEN revision.object_kind = 'literal'
              THEN revision.object_literal #>> '{}'
              ELSE NULL::text
         END
  FROM memory.claim AS claim
  JOIN memory.claim_revision AS revision
    ON revision.owner_user_id = claim.owner_user_id
   AND revision.claim_id = claim.claim_id
   AND revision.revision_id = claim.current_revision_id
   AND revision.revision_number = claim.current_revision_number
  WHERE claim.owner_user_id = actor
    AND claim.claim_id = p_claim_id;
END;
$function$;

REVOKE EXECUTE ON FUNCTION memory_private.read_claim(uuid)
  FROM PUBLIC, governed_memory_worker;
GRANT EXECUTE ON FUNCTION memory_private.read_claim(uuid)
  TO governed_memory_api;
