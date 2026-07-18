BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 local packet review read migration requires sage';
  END IF;
  IF to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'V5 local packet review read prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_review_reader') IS NULL THEN
    CREATE ROLE memory_v5_local_review_reader
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_v5_local_review_reader
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE OR REPLACE FUNCTION memory.read_owner_v5_local_packet_review_v1(
  p_packet_id uuid
)
RETURNS TABLE(
  packet_id uuid,
  operation_id uuid,
  job_id uuid,
  evidence_id uuid,
  evidence_content_sha256 text,
  provider_id text,
  provider_version text,
  provider_model_sha256 text,
  model_file_sha256 text,
  runtime_revision_sha256 text,
  policy_compiler_sha256 text,
  provider_output_sha256 text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  normalized_packet jsonb,
  manual_review_required boolean,
  local_model_calls smallint,
  external_model_calls smallint,
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer,
  packet_created_at timestamptz,
  job_status text,
  job_route text,
  job_attempts integer,
  job_lease_present boolean,
  job_error_present boolean,
  evidence_source_system text,
  evidence_external_id text,
  evidence_content text,
  evidence_authority_sha256 text,
  evidence_status text,
  evidence_recorded_at timestamptz,
  storage_integrity_verified boolean,
  exact_stage_batch_count bigint,
  evidence_stage_batch_count bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  target record;
  calculated_storage_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5 local packet review read requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_packet_id IS NULL THEN
    RAISE EXCEPTION 'packet ID is required'
      USING ERRCODE='22023';
  END IF;

  SELECT
    packet.*,
    job.status::text AS joined_job_status,
    job.route AS joined_job_route,
    job.attempts AS joined_job_attempts,
    (job.lease_token IS NOT NULL OR job.lease_expires_at IS NOT NULL)
      AS joined_job_lease_present,
    (job.last_error IS NOT NULL) AS joined_job_error_present,
    evidence.source_system AS joined_evidence_source_system,
    CASE
      WHEN evidence.external_id ~*
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN lower(evidence.external_id)
      ELSE evidence.evidence_id::text
    END AS joined_evidence_external_id,
    evidence.content AS joined_evidence_content,
    evidence.content_sha256 AS joined_evidence_authority_sha256,
    evidence.status::text AS joined_evidence_status,
    evidence.recorded_at AS joined_evidence_recorded_at
  INTO target
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
   AND job.evidence_id=packet.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_packet_id;

  IF NOT FOUND THEN
    RETURN;
  END IF;
  calculated_storage_sha256 := encode(public.digest(convert_to(
    target.normalized_packet::text,'UTF8'
  ),'sha256'),'hex');
  IF calculated_storage_sha256<>target.packet_storage_sha256 THEN
    RAISE EXCEPTION 'immutable local packet storage hash mismatch'
      USING ERRCODE='23514';
  END IF;

  RETURN QUERY
  SELECT
    target.packet_id,
    target.operation_id,
    target.job_id,
    target.evidence_id,
    target.evidence_content_sha256,
    target.provider_id,
    target.provider_version,
    target.provider_model_sha256,
    target.model_file_sha256,
    target.runtime_revision_sha256,
    target.policy_compiler_sha256,
    target.provider_output_sha256,
    target.validator_packet_sha256,
    target.packet_storage_sha256,
    target.normalized_packet,
    target.manual_review_required,
    target.local_model_calls,
    target.external_model_calls,
    target.entity_mention_count,
    target.observation_count,
    target.comparison_hint_count,
    target.deferral_count,
    target.created_at,
    target.joined_job_status,
    target.joined_job_route,
    target.joined_job_attempts,
    target.joined_job_lease_present,
    target.joined_job_error_present,
    target.joined_evidence_source_system,
    target.joined_evidence_external_id,
    target.joined_evidence_content,
    target.joined_evidence_authority_sha256,
    target.joined_evidence_status,
    target.joined_evidence_recorded_at,
    true,
    (
      SELECT count(*)
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=target.evidence_id
        AND stage.extraction_packet_sha256=target.validator_packet_sha256
    ),
    (
      SELECT count(*)
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=target.evidence_id
    );
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_local_review_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_review_reader;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_review_reader;
GRANT SELECT ON
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,
  memory.evidence,
  memory.relational_stage_batch
TO memory_v5_local_review_reader;

ALTER FUNCTION memory.read_owner_v5_local_packet_review_v1(uuid)
  OWNER TO memory_v5_local_review_reader;

REVOKE ALL ON FUNCTION memory.read_owner_v5_local_packet_review_v1(uuid)
  FROM PUBLIC,brains_app,memory_v5_local_review_reader;
GRANT EXECUTE ON FUNCTION memory.read_owner_v5_local_packet_review_v1(uuid)
  TO brains_app;

COMMIT;
