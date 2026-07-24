BEGIN;

DO $block$
BEGIN
  IF to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.preflight_projection_source_v5_2(uuid)') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.evidence') IS NULL THEN
    RAISE EXCEPTION 'V5.2 projection source prerequisites are absent';
  END IF;
END
$block$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_entailment_source_v5_2(
  p_observation_id uuid
)
RETURNS TABLE(
  observation_id uuid,
  observation_ref text,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  source_spans jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  RETURN QUERY
  SELECT
    observation.observation_id,
    observation.observation_ref,
    observation.observation_sha256,
    observation.evidence_id,
    evidence.content_sha256,
    observation.source_spans
  FROM memory.observation AS observation
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.predicate_registry_version='memory_predicate_registry_v5_2'
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND evidence.content_sha256=memory.v5_digest_text(evidence.content)
    AND subject_entity.status='active'
    AND (
      binding.object_entity_id IS NULL
      OR object_entity.status='active'
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete owner-scoped V5.2 entailment source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

ALTER FUNCTION memory.preflight_projection_entailment_source_v5_2(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.preflight_projection_entailment_source_v5_2(uuid)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_projection_entailment_source_v5_2(uuid)
  TO brains_app;

COMMIT;
