BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regprocedure(
       'memory.owner_batch_has_unentailed_observation_v1(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'reviewed-stage novelty rollback prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_reviewed_observation_stage_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  source_kind text,
  route_event_id uuid,
  atom_apply_id uuid,
  batch_id uuid,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  observation_state_sha256 text,
  observation_count integer,
  source_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'reviewed-observation plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.v5_2_atom_admission_apply AS atom
      ON atom.owner_user_id=route.owner_user_id
     AND atom.packet_id=route.packet_id
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=route.owner_user_id
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=atom.stage_projection_sha256
     AND batch.extractor IN (
       'memory_v1_v5_2_atom_stage_projection',
       'memory_v1_v5_2_atom_stage_projection_v2'
     )
    WHERE route.owner_user_id=actor
    UNION
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=route.owner_user_id
     AND request.request_id=route.request_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=request.owner_user_id
     AND batch.batch_id=(request.result->>'batch_id')::uuid
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=route.validator_packet_sha256
     AND batch.extractor='memory_v1_v5_2_local_packet_review'
    WHERE route.owner_user_id=actor
      AND route.blocking_code_count=0
  )
  SELECT source.source_kind,source.route_event_id,
    source.atom_apply_id,source.batch_id,source.stage_manifest_sha256,
    source.resolution_state_sha256,source.observation_state_sha256,
    source.observation_count,source.source_created_at
  FROM candidate
  CROSS JOIN LATERAL
    memory.v5_2_reviewed_observation_stage_source_v1(
      candidate.route_event_id,candidate.batch_id
    ) AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_stage_admission AS prior
    WHERE prior.owner_user_id=actor
      AND (
        prior.source_route_event_id=source.route_event_id
        OR (
          source.atom_apply_id IS NOT NULL
          AND prior.source_atom_apply_id=source.atom_apply_id
        )
      )
  )
  ORDER BY source.source_created_at,source.route_event_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
TO brains_app;

REVOKE ALL ON FUNCTION
  memory.owner_batch_has_unentailed_observation_v1(uuid)
FROM memory_v5_2_reviewed_observation_stage_maintainer;
DROP FUNCTION memory.owner_batch_has_unentailed_observation_v1(uuid);

COMMIT;
