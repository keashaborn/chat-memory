BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'claim projection registry guard requires sage';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_local_claim_projection_v1(integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.1 claim projection planner is absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_claim_projection_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  assessment_id uuid,
  observation_id uuid,
  observation_sha256 text,
  predicate text,
  assessment_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'local claim projection plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local claim projection plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT assessment.assessment_id,assessment.observation_id,
    assessment.observation_sha256,observation.predicate,
    assessment.created_at
  FROM memory.v5_local_entailment_assessment AS assessment
  JOIN memory.observation AS observation
    ON observation.owner_user_id=assessment.owner_user_id
   AND observation.observation_id=assessment.observation_id
  WHERE assessment.owner_user_id=actor
    AND assessment.governed_decision='accepted'
    AND assessment.raw_decision='entailed'
    AND assessment.confidence='high'
    AND observation.predicate_registry_version='memory_predicate_registry_v5'
    AND observation.predicate IN (
      'identity.name','pet.breed','pet.sex','relationship.has_pet'
    )
    AND observation.projection_class IN ('direct_claim','supportive_context')
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_claim_projection_v1(integer)
  OWNER TO memory_v5_local_projection_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_claim_projection_v1(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_claim_projection_v1(integer) TO brains_app;

COMMIT;
