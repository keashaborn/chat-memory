BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.1 downstream isolation prerequisites are absent';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_disposition AS lane
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=lane.owner_user_id
     AND packet.packet_id=lane.packet_id
    WHERE packet.normalized_packet->>'contract_version'
            IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
       OR packet.normalized_packet->>'predicate_registry_version'
            IS DISTINCT FROM 'memory_predicate_registry_v5'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_review_artifact AS lane
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=lane.owner_user_id
     AND packet.packet_id=lane.packet_id
    WHERE packet.normalized_packet->>'contract_version'
            IS DISTINCT FROM 'memory_v1_relational_extraction_v5'
       OR packet.normalized_packet->>'predicate_registry_version'
            IS DISTINCT FROM 'memory_predicate_registry_v5'
  ) THEN
    RAISE EXCEPTION 'legacy packet lane already contains a non-V5 packet';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  packet_storage_sha256 text,
  disposition_route text,
  reason_code text,
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer,
  packet_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local packet disposition plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local packet disposition plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
    packet.packet_storage_sha256,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
         THEN 'terminal_deferral' ELSE 'manual_review' END,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
         THEN CASE WHEN packet.manual_review_required
              THEN 'deferral_only_review_unresolved'
              ELSE 'deferral_only_no_stage' END
         ELSE 'manual_review_required' END,
    packet.entity_mention_count,
    packet.observation_count,
    packet.comparison_hint_count,
    packet.deferral_count,
    packet.created_at
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id=actor
    AND packet.normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5'
    AND packet.normalized_packet->>'predicate_registry_version'
          ='memory_predicate_registry_v5'
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id=actor
        AND supersession.prior_packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_review_artifact AS artifact
      WHERE artifact.owner_user_id=actor
        AND artifact.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=packet.evidence_id
    )
  ORDER BY
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
         THEN 0 ELSE 1 END,
    packet.created_at,
    packet.packet_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_packet_disposition_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_packet_disposition_v1(integer)
  TO brains_app;

CREATE OR REPLACE FUNCTION memory.guard_v5_legacy_packet_lane_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.packet_id IS NULL THEN
    RAISE EXCEPTION 'legacy V5 packet lane accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=NEW.owner_user_id
      AND packet.packet_id=NEW.packet_id
      AND packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5'
  ) THEN
    RAISE EXCEPTION 'non-V5 packet cannot enter the legacy packet lane'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;

DROP TRIGGER IF EXISTS v5_local_disposition_contract_guard
  ON memory.v5_local_packet_disposition;
CREATE TRIGGER v5_local_disposition_contract_guard
BEFORE INSERT ON memory.v5_local_packet_disposition
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_legacy_packet_lane_v1();

DROP TRIGGER IF EXISTS v5_local_review_artifact_contract_guard
  ON memory.v5_local_packet_review_artifact;
CREATE TRIGGER v5_local_review_artifact_contract_guard
BEFORE INSERT ON memory.v5_local_packet_review_artifact
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_legacy_packet_lane_v1();

COMMIT;
