BEGIN;
SET LOCAL lock_timeout='5s';
DROP FUNCTION IF EXISTS memory.record_owner_v5_local_review_artifact_v1(
  uuid,uuid,uuid,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_local_packet_disposition_v1(integer);
DROP TABLE IF EXISTS memory.v5_local_packet_review_artifact;

CREATE FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(
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
SET search_path=pg_catalog
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
    packet.packet_id,packet.job_id,packet.evidence_id,
    packet.packet_storage_sha256,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required
         THEN 'terminal_deferral' ELSE 'manual_review' END,
    CASE WHEN packet.entity_mention_count=0
                   AND packet.observation_count=0
                   AND packet.comparison_hint_count=0
                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required
         THEN 'deferral_only_no_stage' ELSE 'manual_review_required' END,
    packet.entity_mention_count,packet.observation_count,
    packet.comparison_hint_count,packet.deferral_count,packet.created_at
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id=actor
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
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
                   AND NOT packet.manual_review_required
         THEN 0 ELSE 1 END,
    packet.created_at,packet.packet_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  TO brains_app;
ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer,memory_v5_local_disposition_maintainer;
COMMIT;
