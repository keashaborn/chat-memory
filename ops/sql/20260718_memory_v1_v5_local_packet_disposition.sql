BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $migration$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local packet disposition migration requires sage';
  END IF;
  IF to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_local_inference_append_only()') IS NULL THEN
    RAISE EXCEPTION 'local packet disposition prerequisites are absent';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memory_v5_local_disposition_maintainer') THEN
    CREATE ROLE memory_v5_local_disposition_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS memory.v5_local_packet_disposition (
  disposition_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  disposition text NOT NULL CHECK (disposition='terminal_no_stage'),
  reason_code text NOT NULL CHECK (reason_code='deferral_only_no_stage'),
  evidence_content_sha256 text NOT NULL
    CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  validator_packet_sha256 text NOT NULL
    CHECK (validator_packet_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  entity_mention_count smallint NOT NULL CHECK (entity_mention_count=0),
  observation_count smallint NOT NULL CHECK (observation_count=0),
  comparison_hint_count smallint NOT NULL CHECK (comparison_hint_count=0),
  deferral_count smallint NOT NULL CHECK (deferral_count BETWEEN 1 AND 32),
  local_model_calls smallint NOT NULL CHECK (local_model_calls=1),
  external_model_calls smallint NOT NULL CHECK (external_model_calls=0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
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

CREATE INDEX IF NOT EXISTS v5_local_packet_disposition_owner_time_idx
  ON memory.v5_local_packet_disposition(owner_user_id,created_at,disposition_id);

ALTER TABLE memory.v5_local_packet_disposition OWNER TO sage;
ALTER TABLE memory.v5_local_packet_disposition ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_packet_disposition FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.v5_local_packet_disposition;
CREATE POLICY owner_isolation ON memory.v5_local_packet_disposition
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DROP TRIGGER IF EXISTS v5_local_packet_disposition_append_only_guard
  ON memory.v5_local_packet_disposition;
CREATE TRIGGER v5_local_packet_disposition_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_packet_disposition
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

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  p_operation_id uuid,
  p_disposition_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_reason_code text
)
RETURNS TABLE(
  disposition_id uuid,
  packet_id uuid,
  disposition text,
  reason_code text,
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
  replayed memory.v5_local_packet_disposition%ROWTYPE;
  calculated_storage_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local packet disposition apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_disposition_id IS NULL OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_code<>'deferral_only_no_stage' THEN
    RAISE EXCEPTION 'local packet disposition inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_packet_disposition',actor::text,
      p_packet_id::text),0
  ));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_disposition AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.disposition_id<>p_disposition_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.disposition<>'terminal_no_stage'
       OR replayed.reason_code<>p_reason_code THEN
      RAISE EXCEPTION 'local packet disposition replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.disposition_id,replayed.packet_id,
      replayed.disposition,replayed.reason_code,'replayed'::text;
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
     OR packet.validator_packet_sha256 IS NULL
     OR packet.provider_id<>'local_llama_cpp'
     OR packet.local_model_calls<>1 OR packet.external_model_calls<>0
     OR packet.manual_review_required
     OR packet.entity_mention_count<>0 OR packet.observation_count<>0
     OR packet.comparison_hint_count<>0 OR packet.deferral_count<1
     OR jsonb_typeof(packet.normalized_packet)<>'object'
     OR jsonb_array_length(packet.normalized_packet->'entity_mentions')<>0
     OR jsonb_array_length(packet.normalized_packet->'observations')<>0
     OR jsonb_array_length(packet.normalized_packet->'comparison_hints')<>0
     OR jsonb_array_length(packet.normalized_packet->'deferrals')
        <>packet.deferral_count
     OR packet.job_status<>'review_required'
     OR packet.job_route<>'relational_extraction'
     OR packet.lease_token IS NOT NULL OR packet.lease_expires_at IS NOT NULL
     OR packet.last_error IS NOT NULL
     OR packet.evidence_status<>'active'
     OR packet.authority_sha256<>packet.evidence_content_sha256
     OR EXISTS (
       SELECT 1 FROM memory.relational_stage_batch AS stage
       WHERE stage.owner_user_id=actor AND stage.evidence_id=packet.evidence_id
     ) THEN
    RAISE EXCEPTION 'local packet is not an eligible terminal deferral'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_disposition(
    disposition_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    disposition,reason_code,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,
    entity_mention_count,observation_count,comparison_hint_count,
    deferral_count,local_model_calls,external_model_calls
  ) VALUES (
    p_disposition_id,actor,p_operation_id,p_packet_id,packet.job_id,
    packet.evidence_id,'terminal_no_stage',p_reason_code,
    packet.evidence_content_sha256,packet.validator_packet_sha256,
    packet.packet_storage_sha256,packet.entity_mention_count,
    packet.observation_count,packet.comparison_hint_count,
    packet.deferral_count,packet.local_model_calls,packet.external_model_calls
  );

  RETURN QUERY SELECT p_disposition_id,p_packet_id,'terminal_no_stage'::text,
    p_reason_code,'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_disposition_maintainer;
GRANT SELECT ON memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,memory.evidence,
  memory.relational_stage_batch,memory.v5_local_packet_disposition
TO memory_v5_local_disposition_maintainer;
GRANT INSERT ON memory.v5_local_packet_disposition
TO memory_v5_local_disposition_maintainer;

ALTER FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) OWNER TO memory_v5_local_disposition_maintainer;

REVOKE ALL ON memory.v5_local_packet_disposition FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
) TO brains_app;

COMMIT;
