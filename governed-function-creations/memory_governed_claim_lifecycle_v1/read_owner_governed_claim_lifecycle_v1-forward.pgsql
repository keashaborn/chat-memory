CREATE FUNCTION memory.read_owner_governed_claim_lifecycle_v1(p_limit integer)
RETURNS TABLE(
  claim_id uuid,
  status text,
  current_revision_number integer,
  canonical_text text,
  predicate text,
  confidence numeric,
  sensitivity text,
  updated_at timestamptz,
  claim_state_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'governed claim lifecycle limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    claim.claim_id,
    claim.status::text,
    (state.value->>'current_revision_number')::integer,
    claim.canonical_text,
    claim.predicate,
    claim.confidence,
    claim.sensitivity::text,
    claim.updated_at,
    state.value->>'claim_state_sha256'
  FROM memory.claim AS claim
  CROSS JOIN LATERAL (
    SELECT memory.claim_assessment_state_v5(claim.claim_id) AS value
  ) AS state
  WHERE claim.owner_user_id=actor
    AND claim.canonical_key LIKE 'v5:%'
    AND claim.metadata->>'memory_contract'='memory_projection_v5'
  ORDER BY claim.updated_at DESC,claim.claim_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.read_owner_governed_claim_lifecycle_v1(integer) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.read_owner_governed_claim_lifecycle_v1(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.read_owner_governed_claim_lifecycle_v1(integer) TO brains_app;
