BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local auto-stage admission migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure(
       'memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)'
     ) IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL THEN
    RAISE EXCEPTION 'local auto-stage admission prerequisites are absent';
  END IF;
END
$preflight$;

DO $constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.v5_local_packet_review_artifact'::regclass
      AND conname='v5_local_packet_review_artifact_owner_artifact_key'
  ) THEN
    ALTER TABLE memory.v5_local_packet_review_artifact
      ADD CONSTRAINT v5_local_packet_review_artifact_owner_artifact_key
      UNIQUE(owner_user_id,artifact_id);
  END IF;
END
$constraint$;

CREATE TABLE IF NOT EXISTS memory.v5_local_packet_stage_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stage_request_id uuid NOT NULL,
  review_report_sha256 text NOT NULL
    CHECK (review_report_sha256 ~ '^[0-9a-f]{64}$'),
  stage_bundle_sha256 text NOT NULL
    CHECK (stage_bundle_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  repository_commit text NOT NULL
    CHECK (repository_commit ~ '^[0-9a-f]{40}$'),
  policy_version text NOT NULL
    CHECK (policy_version='memory_v1_v5_local_auto_stage_policy_v1'),
  decision text NOT NULL CHECK (decision='auto_stage_eligible'),
  entity_mention_count smallint NOT NULL
    CHECK (entity_mention_count BETWEEN 0 AND 24),
  observation_count smallint NOT NULL
    CHECK (observation_count BETWEEN 0 AND 32),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,artifact_id),
  UNIQUE(owner_user_id,packet_id),
  UNIQUE(owner_user_id,stage_request_id),
  FOREIGN KEY(owner_user_id,artifact_id)
    REFERENCES memory.v5_local_packet_review_artifact(owner_user_id,artifact_id)
    ON DELETE RESTRICT,
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

CREATE INDEX IF NOT EXISTS v5_local_packet_stage_admission_owner_time_idx
  ON memory.v5_local_packet_stage_admission(owner_user_id,created_at,admission_id);
ALTER TABLE memory.v5_local_packet_stage_admission OWNER TO sage;
ALTER TABLE memory.v5_local_packet_stage_admission ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_packet_stage_admission FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_packet_stage_admission;
CREATE POLICY owner_isolation ON memory.v5_local_packet_stage_admission
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_packet_stage_admission_append_only_guard
  ON memory.v5_local_packet_stage_admission;
CREATE TRIGGER v5_local_packet_stage_admission_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_packet_stage_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_auto_stage_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  artifact_id uuid,
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  stage_request_id uuid,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  packet_storage_sha256 text,
  repository_commit text,
  auto_link_count integer,
  entity_mention_count integer,
  observation_count integer,
  artifact_created_at timestamptz
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
    RAISE EXCEPTION 'local auto-stage plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local auto-stage plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    artifact.artifact_id,artifact.packet_id,artifact.job_id,
    artifact.evidence_id,artifact.request_id,
    artifact.review_report_sha256,artifact.stage_bundle_sha256,
    artifact.packet_storage_sha256,artifact.repository_commit,
    artifact.auto_link_count::integer,packet.entity_mention_count,
    packet.observation_count,artifact.created_at
  FROM memory.v5_local_packet_review_artifact AS artifact
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=artifact.owner_user_id
   AND packet.packet_id=artifact.packet_id
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=artifact.owner_user_id
   AND job.job_id=artifact.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=artifact.owner_user_id
   AND evidence.evidence_id=artifact.evidence_id
  WHERE artifact.owner_user_id=actor
    AND artifact.review_disposition='manual_review_required'
    AND artifact.manual_review_count=0
    AND artifact.deferred_count=0
    AND artifact.rejected_count=0
    AND artifact.blocking_code_count=0
    AND artifact.auto_link_count=packet.entity_mention_count
    AND packet.comparison_hint_count=0 AND packet.deferral_count=0
    AND packet.local_model_calls=1 AND packet.external_model_calls=0
    AND packet.provider_id='local_llama_cpp'
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND evidence.content_sha256=packet.evidence_content_sha256
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_stage_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.artifact_id=artifact.artifact_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=artifact.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=artifact.evidence_id
    )
  ORDER BY artifact.created_at,artifact.artifact_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_auto_stage_v1(
  p_operation_id uuid,
  p_admission_id uuid,
  p_artifact_id uuid,
  p_expected_review_report_sha256 text,
  p_expected_stage_bundle_sha256 text,
  p_expected_packet_storage_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  artifact_id uuid,
  packet_id uuid,
  stage_request_id uuid,
  decision text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  source record;
  replayed memory.v5_local_packet_stage_admission%ROWTYPE;
  calculated_storage_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local auto-stage admission requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_admission_id IS NULL OR p_artifact_id IS NULL
     OR p_expected_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>'memory_v1_v5_local_auto_stage_policy_v1' THEN
    RAISE EXCEPTION 'local auto-stage admission inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_auto_stage',actor::text,
      p_artifact_id::text),0
  ));
  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_stage_admission AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id
      OR value.admission_id=p_admission_id
      OR value.artifact_id=p_artifact_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.admission_id<>p_admission_id
       OR replayed.artifact_id<>p_artifact_id
       OR replayed.review_report_sha256<>p_expected_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_expected_stage_bundle_sha256
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local auto-stage admission replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.admission_id,replayed.artifact_id,
      replayed.packet_id,replayed.stage_request_id,replayed.decision,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT
    artifact.*,
    packet.normalized_packet,packet.entity_mention_count,
    packet.observation_count,packet.comparison_hint_count,
    packet.deferral_count,packet.local_model_calls,packet.external_model_calls,
    packet.provider_id,packet.evidence_content_sha256,
    job.status::text AS job_status,job.route AS job_route,
    job.lease_token,job.lease_expires_at,job.last_error,
    evidence.status::text AS evidence_status,
    evidence.content_sha256 AS authority_sha256
  INTO source
  FROM memory.v5_local_packet_review_artifact AS artifact
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=artifact.owner_user_id
   AND packet.packet_id=artifact.packet_id
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=artifact.owner_user_id
   AND job.job_id=artifact.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=artifact.owner_user_id
   AND evidence.evidence_id=artifact.evidence_id
  WHERE artifact.owner_user_id=actor
    AND artifact.artifact_id=p_artifact_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped local review artifact is absent'
      USING ERRCODE='23514';
  END IF;

  calculated_storage_sha256 := encode(public.digest(convert_to(
    source.normalized_packet::text,'UTF8'
  ),'sha256'),'hex');
  IF source.review_report_sha256<>p_expected_review_report_sha256
     OR source.stage_bundle_sha256<>p_expected_stage_bundle_sha256
     OR source.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR calculated_storage_sha256<>source.packet_storage_sha256
     OR source.review_disposition<>'manual_review_required'
     OR source.manual_review_count<>0 OR source.deferred_count<>0
     OR source.rejected_count<>0 OR source.blocking_code_count<>0
     OR source.auto_link_count<>source.entity_mention_count
     OR source.comparison_hint_count<>0 OR source.deferral_count<>0
     OR source.local_model_calls<>1 OR source.external_model_calls<>0
     OR source.provider_id<>'local_llama_cpp'
     OR source.job_status<>'review_required'
     OR source.job_route<>'relational_extraction'
     OR source.lease_token IS NOT NULL OR source.lease_expires_at IS NOT NULL
     OR source.last_error IS NOT NULL OR source.evidence_status<>'active'
     OR source.authority_sha256<>source.evidence_content_sha256
     OR EXISTS (
       SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
       WHERE disposition.owner_user_id=actor
         AND disposition.packet_id=source.packet_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id=actor
         AND stage.evidence_id=source.evidence_id
     ) THEN
    RAISE EXCEPTION 'local review artifact is not auto-stage eligible'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_stage_admission(
    admission_id,owner_user_id,operation_id,artifact_id,packet_id,job_id,
    evidence_id,stage_request_id,review_report_sha256,stage_bundle_sha256,
    packet_storage_sha256,repository_commit,policy_version,decision,
    entity_mention_count,observation_count
  ) VALUES (
    p_admission_id,actor,p_operation_id,p_artifact_id,source.packet_id,
    source.job_id,source.evidence_id,source.request_id,
    p_expected_review_report_sha256,p_expected_stage_bundle_sha256,
    p_expected_packet_storage_sha256,source.repository_commit,p_policy_version,
    'auto_stage_eligible',source.entity_mention_count,source.observation_count
  );
  RETURN QUERY SELECT p_admission_id,p_artifact_id,source.packet_id,
    source.request_id,'auto_stage_eligible'::text,'applied'::text;
END
$function$;

GRANT SELECT,INSERT ON memory.v5_local_packet_stage_admission
  TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_auto_stage_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_auto_stage_v1(
  uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON memory.v5_local_packet_stage_admission FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_auto_stage_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_auto_stage_v1(
  uuid,uuid,uuid,text,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_auto_stage_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_auto_stage_v1(
  uuid,uuid,uuid,text,text,text,text
) TO brains_app;

COMMIT;
