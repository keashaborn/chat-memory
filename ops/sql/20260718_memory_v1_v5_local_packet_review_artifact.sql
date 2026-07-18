BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
DECLARE
  policy_roles oid[];
  writer_oid oid;
  disposition_oid oid;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local packet review artifact migration requires sage';
  END IF;
  IF to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('memory_v5_local_review_reader') IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL THEN
    RAISE EXCEPTION 'local packet review artifact prerequisites are absent';
  END IF;
  SELECT oid INTO writer_oid FROM pg_roles WHERE rolname='memory_v5_writer';
  SELECT oid INTO disposition_oid FROM pg_roles
  WHERE rolname='memory_v5_local_disposition_maintainer';
  SELECT polroles INTO policy_roles FROM pg_policy
  WHERE polrelid='memory.relational_stage_batch'::regclass
    AND polname='owner_isolation';
  IF cardinality(policy_roles) NOT IN (2,3)
     OR NOT (writer_oid=ANY(policy_roles))
     OR NOT (disposition_oid=ANY(policy_roles)) THEN
    RAISE EXCEPTION 'stage owner policy is not disposition-compatible';
  END IF;
END
$preflight$;

ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer,memory_v5_local_disposition_maintainer,
     memory_v5_local_review_reader;

CREATE TABLE IF NOT EXISTS memory.v5_local_packet_review_artifact (
  artifact_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  review_id uuid NOT NULL,
  request_id uuid NOT NULL,
  review_report_sha256 text NOT NULL
    CHECK (review_report_sha256 ~ '^[0-9a-f]{64}$'),
  stage_bundle_sha256 text NOT NULL
    CHECK (stage_bundle_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  repository_commit text NOT NULL
    CHECK (repository_commit ~ '^[0-9a-f]{40}$'),
  auto_link_count smallint NOT NULL CHECK (auto_link_count BETWEEN 0 AND 32),
  manual_review_count smallint NOT NULL
    CHECK (manual_review_count BETWEEN 0 AND 32),
  deferred_count smallint NOT NULL CHECK (deferred_count BETWEEN 0 AND 32),
  rejected_count smallint NOT NULL CHECK (rejected_count BETWEEN 0 AND 32),
  blocking_code_count smallint NOT NULL
    CHECK (blocking_code_count BETWEEN 0 AND 32),
  review_disposition text NOT NULL
    CHECK (review_disposition='manual_review_required'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
  UNIQUE(owner_user_id,review_id),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(owner_user_id,packet_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS v5_local_packet_review_artifact_owner_time_idx
  ON memory.v5_local_packet_review_artifact(owner_user_id,created_at,artifact_id);

ALTER TABLE memory.v5_local_packet_review_artifact OWNER TO sage;
ALTER TABLE memory.v5_local_packet_review_artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_packet_review_artifact FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.v5_local_packet_review_artifact;
CREATE POLICY owner_isolation ON memory.v5_local_packet_review_artifact
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_packet_review_artifact_append_only_guard
  ON memory.v5_local_packet_review_artifact;
CREATE TRIGGER v5_local_packet_review_artifact_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_packet_review_artifact
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

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
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
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
    packet.entity_mention_count,
    packet.observation_count,
    packet.comparison_hint_count,
    packet.deferral_count,
    packet.created_at
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
                   AND NOT packet.manual_review_required
         THEN 0 ELSE 1 END,
    packet.created_at,
    packet.packet_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_owner_v5_local_review_artifact_v1(
  p_operation_id uuid,
  p_artifact_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_review_id uuid,
  p_request_id uuid,
  p_review_report_sha256 text,
  p_stage_bundle_sha256 text,
  p_repository_commit text,
  p_auto_link_count integer,
  p_manual_review_count integer,
  p_deferred_count integer,
  p_rejected_count integer,
  p_blocking_code_count integer
)
RETURNS TABLE(
  artifact_id uuid,
  packet_id uuid,
  review_disposition text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  packet record;
  replayed memory.v5_local_packet_review_artifact%ROWTYPE;
  calculated_storage_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local review artifact apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_artifact_id IS NULL OR p_packet_id IS NULL
     OR p_review_id IS NULL OR p_request_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_repository_commit !~ '^[0-9a-f]{40}$'
     OR p_auto_link_count NOT BETWEEN 0 AND 32
     OR p_manual_review_count NOT BETWEEN 0 AND 32
     OR p_deferred_count NOT BETWEEN 0 AND 32
     OR p_rejected_count NOT BETWEEN 0 AND 32
     OR p_blocking_code_count NOT BETWEEN 0 AND 32 THEN
    RAISE EXCEPTION 'local review artifact inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_review_artifact',actor::text,
      p_packet_id::text),0
  ));
  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_review_artifact AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.artifact_id<>p_artifact_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.review_id<>p_review_id
       OR replayed.request_id<>p_request_id
       OR replayed.review_report_sha256<>p_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_stage_bundle_sha256
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.repository_commit<>p_repository_commit
       OR replayed.auto_link_count<>p_auto_link_count
       OR replayed.manual_review_count<>p_manual_review_count
       OR replayed.deferred_count<>p_deferred_count
       OR replayed.rejected_count<>p_rejected_count
       OR replayed.blocking_code_count<>p_blocking_code_count THEN
      RAISE EXCEPTION 'local review artifact replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.artifact_id,replayed.packet_id,
      replayed.review_disposition,'replayed'::text;
    RETURN;
  END IF;

  SELECT
    local_packet.*,
    job.status::text AS job_status,
    job.route AS job_route,
    job.lease_token,
    job.lease_expires_at,
    job.last_error,
    evidence.status::text AS evidence_status,
    evidence.content_sha256 AS authority_sha256
  INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS local_packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=local_packet.owner_user_id
   AND job.job_id=local_packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=local_packet.owner_user_id
   AND evidence.evidence_id=local_packet.evidence_id
  WHERE local_packet.owner_user_id=actor
    AND local_packet.packet_id=p_packet_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped local packet is absent'
      USING ERRCODE='23514';
  END IF;
  calculated_storage_sha256 := encode(public.digest(convert_to(
    packet.normalized_packet::text,'UTF8'
  ),'sha256'),'hex');
  IF packet.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR calculated_storage_sha256<>packet.packet_storage_sha256
     OR packet.provider_id<>'local_llama_cpp'
     OR packet.local_model_calls<>1 OR packet.external_model_calls<>0
     OR NOT packet.manual_review_required
     OR packet.entity_mention_count+packet.observation_count
        +packet.comparison_hint_count<1
     OR p_auto_link_count+p_manual_review_count+p_deferred_count
        +p_rejected_count<>packet.entity_mention_count
     OR packet.job_status<>'review_required'
     OR packet.job_route<>'relational_extraction'
     OR packet.lease_token IS NOT NULL OR packet.lease_expires_at IS NOT NULL
     OR packet.last_error IS NOT NULL
     OR packet.evidence_status<>'active'
     OR packet.authority_sha256<>packet.evidence_content_sha256
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id=actor AND stage.evidence_id=packet.evidence_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
       WHERE disposition.owner_user_id=actor
         AND disposition.packet_id=packet.packet_id
     ) THEN
    RAISE EXCEPTION 'local packet is not eligible for a review artifact'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_review_artifact(
    artifact_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    review_id,request_id,review_report_sha256,stage_bundle_sha256,
    packet_storage_sha256,repository_commit,auto_link_count,
    manual_review_count,deferred_count,rejected_count,blocking_code_count,
    review_disposition
  ) VALUES (
    p_artifact_id,actor,p_operation_id,p_packet_id,packet.job_id,
    packet.evidence_id,p_review_id,p_request_id,p_review_report_sha256,
    p_stage_bundle_sha256,p_expected_packet_storage_sha256,
    p_repository_commit,p_auto_link_count,p_manual_review_count,
    p_deferred_count,p_rejected_count,p_blocking_code_count,
    'manual_review_required'
  );
  RETURN QUERY SELECT p_artifact_id,p_packet_id,
    'manual_review_required'::text,'applied'::text;
END
$function$;

GRANT SELECT,INSERT ON memory.v5_local_packet_review_artifact
  TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.record_owner_v5_local_review_artifact_v1(
  uuid,uuid,uuid,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON memory.v5_local_packet_review_artifact FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.record_owner_v5_local_review_artifact_v1(
  uuid,uuid,uuid,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_v5_local_review_artifact_v1(
  uuid,uuid,uuid,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) TO brains_app;

COMMIT;
