BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole(
       'memory_v5_2_reviewed_observation_stage_maintainer'
     ) IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)'
     ) IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_entailment_v5') IS NULL THEN
    RAISE EXCEPTION 'reviewed-stage novelty prerequisites are absent';
  END IF;
END
$preflight$;

DO $rollback_guard$
BEGIN
  IF EXISTS(
    SELECT 1 FROM memory.observation
    WHERE predicate='residence.care_setting'
  ) THEN
    RAISE EXCEPTION
      'refusing to remove a compiler extension used by observations'
      USING ERRCODE='55000';
  END IF;
END
$rollback_guard$;

DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_observation_conflict_v1(integer);
DROP FUNCTION IF EXISTS
  memory.owner_batch_has_possible_temporal_update_v1(uuid);
DROP FUNCTION IF EXISTS
  memory.owner_batch_possible_temporal_updates_v1(uuid);
DROP FUNCTION IF EXISTS
  memory.v5_2_observation_semantic_slot_v1(text,jsonb);

DELETE FROM memory.predicate_contract
WHERE registry_version='memory_predicate_registry_v5_2'
  AND predicate='residence.care_setting';
DELETE FROM memory.predicate_registry_seed
WHERE registry_version='memory_predicate_registry_v5_2'
  AND predicate='residence.care_setting';
DELETE FROM memory.predicate
WHERE predicate='residence.care_setting'
  AND NOT EXISTS(
    SELECT 1 FROM memory.predicate_contract
    WHERE predicate='residence.care_setting'
  );
DROP TABLE IF EXISTS memory.predicate_registry_compiler_extension_v1;
DROP FUNCTION IF EXISTS
  memory.reject_predicate_registry_compiler_extension_v1_mutation();

CREATE OR REPLACE FUNCTION
memory.owner_batch_has_unentailed_observation_v1(p_batch_id uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'batch novelty check requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_batch_id IS NULL THEN
    RAISE EXCEPTION 'batch ID is required'
      USING ERRCODE='22004';
  END IF;

  RETURN EXISTS (
    SELECT 1
    FROM memory.relational_stage_batch AS batch
    CROSS JOIN LATERAL jsonb_each_text(
      batch.result->'observation_ids'
    ) AS ids
    JOIN memory.observation AS observation
      ON observation.owner_user_id=batch.owner_user_id
     AND observation.observation_id=ids.value::uuid
     AND observation.evidence_id=batch.evidence_id
     AND observation.observation_ref=ids.key
     AND observation.packet_sha256=batch.extraction_packet_sha256
    WHERE batch.owner_user_id=actor
      AND batch.batch_id=p_batch_id
      AND jsonb_typeof(batch.result->'observation_ids')='object'
      AND observation.projection_class<>'never_surface'
      AND NOT EXISTS (
        SELECT 1
        FROM memory.observation_entailment_v5 AS entailment
        WHERE entailment.owner_user_id=observation.owner_user_id
          AND entailment.observation_id=observation.observation_id
      )
  );
END
$function$;

ALTER FUNCTION memory.owner_batch_has_unentailed_observation_v1(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.owner_batch_has_unentailed_observation_v1(uuid)
FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.owner_batch_has_unentailed_observation_v1(uuid)
TO memory_v5_2_reviewed_observation_stage_maintainer;

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
    AND memory.owner_batch_has_unentailed_observation_v1(
      source.batch_id
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

DO $postflight$
BEGIN
  IF has_function_privilege(
       'brains_app',
       'memory.owner_batch_has_unentailed_observation_v1(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_v5_2_reviewed_observation_stage_maintainer',
       'memory.owner_batch_has_unentailed_observation_v1(uuid)',
       'EXECUTE'
     )
     OR (
       SELECT proowner::regrole::text<>'memory_v5_writer'
       FROM pg_proc
       WHERE oid=
         'memory.owner_batch_has_unentailed_observation_v1(uuid)'::regprocedure
     ) THEN
    RAISE EXCEPTION 'reviewed-stage novelty helper ACL is unsafe';
  END IF;
END
$postflight$;

COMMIT;
