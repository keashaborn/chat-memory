BEGIN;

DO $preflight$
DECLARE
  required_table text;
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence intake selector migration requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure(
       'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)'
     ) IS NULL THEN
    RAISE EXCEPTION 'evidence intake selector prerequisites are absent';
  END IF;
  FOREACH required_table IN ARRAY ARRAY[
    'evidence',
    'artifact_endorsement',
    'artifact_occurrence',
    'candidate',
    'claim_evidence',
    'entity_alias',
    'entity_mention',
    'evidence_ingest_batch_row',
    'observation_entailment_v5',
    'preference_candidate_evidence',
    'preference_revision_evidence',
    'project_knowledge_candidate_evidence',
    'project_knowledge_revision_evidence',
    'relational_stage_batch',
    'user_preference',
    'consolidation_job'
  ]
  LOOP
    IF to_regclass(format('memory.%I',required_table)) IS NULL THEN
      RAISE EXCEPTION 'required table memory.% is absent', required_table;
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
      WHERE namespace.nspname='memory'
        AND relation.relname=required_table
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'required table memory.% is not protected by forced RLS',
        required_table;
    END IF;
  END LOOP;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_intake_maintainer') IS NULL THEN
    CREATE ROLE memory_intake_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_intake_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.evidence_intake_terminal (
  terminal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  selector_version text NOT NULL,
  outcome text NOT NULL,
  reason_code text NOT NULL,
  evidence_content_sha256 text,
  source_job_id uuid,
  source_job_status memory.consolidation_job_status,
  source_job_pipeline_version text,
  decision_fingerprint text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_role name NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, terminal_id),
  UNIQUE (owner_user_id, evidence_id, selector_version),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, source_job_id)
    REFERENCES memory.consolidation_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  CHECK (actor_user_id=owner_user_id),
  CHECK (selector_version ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'),
  CHECK (outcome IN ('empty','skipped')),
  CHECK (
    reason_code IN (
      'empty_content',
      'missing_content_hash',
      'upstream_completed_empty',
      'upstream_skipped',
      'upstream_review_required',
      'upstream_link_inconsistency'
    )
  ),
  CHECK (
    evidence_content_sha256 IS NULL
    OR evidence_content_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CHECK (
    decision_fingerprint ~ '^[0-9a-f]{64}$'
  ),
  CHECK (
    source_job_id IS NOT NULL
    OR (
      source_job_status IS NULL
      AND source_job_pipeline_version IS NULL
    )
  ),
  CHECK (jsonb_typeof(details)='object'),
  CHECK (pg_column_size(details)<=8192)
);

CREATE INDEX IF NOT EXISTS evidence_intake_terminal_owner_outcome_time_idx
  ON memory.evidence_intake_terminal(
    owner_user_id,selector_version,outcome,created_at,evidence_id
  );

CREATE INDEX IF NOT EXISTS artifact_endorsement_owner_evidence_idx
  ON memory.artifact_endorsement(owner_user_id,evidence_id);
CREATE INDEX IF NOT EXISTS entity_alias_owner_evidence_idx
  ON memory.entity_alias(owner_user_id,evidence_id)
  WHERE evidence_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS user_preference_owner_evidence_idx
  ON memory.user_preference(owner_user_id,evidence_id)
  WHERE evidence_id IS NOT NULL;

ALTER TABLE memory.evidence_intake_terminal OWNER TO sage;
ALTER TABLE memory.evidence_intake_terminal ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_intake_terminal FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_intake_terminal;
CREATE POLICY owner_isolation ON memory.evidence_intake_terminal
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.guard_evidence_intake_terminal_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.evidence_intake_terminal is append-only'
    USING ERRCODE='42501';
END
$function$;

DROP TRIGGER IF EXISTS evidence_intake_terminal_append_only_guard
  ON memory.evidence_intake_terminal;
CREATE TRIGGER evidence_intake_terminal_append_only_guard
BEFORE UPDATE OR DELETE ON memory.evidence_intake_terminal
FOR EACH ROW
EXECUTE FUNCTION memory.guard_evidence_intake_terminal_append_only();

DO $function_ownership$
BEGIN
  IF to_regprocedure(
    'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.plan_owner_evidence_intake_v1(text,integer,uuid)
      OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.record_owner_evidence_intake_terminal_v1(uuid,text,text,text,text)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.record_owner_evidence_intake_terminal_v1(
      uuid,text,text,text,text
    ) OWNER TO sage;
  END IF;
END
$function_ownership$;

CREATE OR REPLACE FUNCTION memory.plan_owner_evidence_intake_v1(
  p_selector_version text,
  p_limit integer DEFAULT 100,
  p_evidence_id uuid DEFAULT NULL
)
RETURNS TABLE(
  evidence_id uuid,
  evidence_content_sha256 text,
  outcome text,
  route text,
  reason_code text,
  source_job_id uuid,
  source_job_status text,
  source_job_pipeline_version text,
  upstream_candidate_count integer
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
    RAISE EXCEPTION 'evidence intake plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_selector_version IS NULL
     OR p_selector_version !~ '^[a-z0-9][a-z0-9_.-]{2,63}$'
     OR p_limit IS NULL
     OR p_limit<1
     OR p_limit>500 THEN
    RAISE EXCEPTION 'evidence intake plan inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH unaccounted AS (
    SELECT
      evidence.evidence_id,
      evidence.kind,
      evidence.external_id,
      evidence.source_system,
      evidence.content,
      evidence.content_sha256,
      evidence.recorded_at
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.status='active'
      AND (p_evidence_id IS NULL OR evidence.evidence_id=p_evidence_id)
      AND NOT EXISTS (
        SELECT 1
        FROM memory.evidence_intake_terminal AS terminal
        WHERE terminal.owner_user_id=actor
          AND terminal.evidence_id=evidence.evidence_id
          AND terminal.selector_version=p_selector_version
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.artifact_endorsement AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.artifact_occurrence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.candidate AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.claim_evidence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.entity_alias AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.entity_mention AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.evidence_ingest_batch_row AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.observation_entailment_v5 AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.preference_candidate_evidence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.preference_revision_evidence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.project_knowledge_candidate_evidence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.project_knowledge_revision_evidence AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.relational_stage_batch AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.user_preference AS link
        WHERE link.owner_user_id=actor
          AND link.evidence_id=evidence.evidence_id
      )
  ),
  with_job AS (
    SELECT
      unaccounted.*,
      job.job_id,
      job.status AS job_status,
      job.pipeline_version,
      job.result,
      COALESCE(
        CASE
          WHEN jsonb_typeof(job.result->'claim_candidate_ids')='array'
          THEN jsonb_array_length(job.result->'claim_candidate_ids')
          ELSE 0
        END,
        0
      )
      + COALESCE(
        CASE
          WHEN jsonb_typeof(job.result->'preference_candidate_ids')='array'
          THEN jsonb_array_length(job.result->'preference_candidate_ids')
          ELSE 0
        END,
        0
      )
      + COALESCE(
        CASE
          WHEN jsonb_typeof(job.result->'project_candidate_ids')='array'
          THEN jsonb_array_length(job.result->'project_candidate_ids')
          ELSE 0
        END,
        0
      ) AS candidate_count
    FROM unaccounted
    LEFT JOIN LATERAL (
      SELECT source_job.*
      FROM memory.consolidation_job AS source_job
      WHERE source_job.owner_user_id=actor
        AND source_job.source_system=unaccounted.source_system
        AND (
          source_job.source_external_id=unaccounted.external_id
          OR 'chat_log:' || source_job.source_external_id=
             unaccounted.external_id
          OR source_job.source_external_id=
             'chat_log:' || unaccounted.external_id
        )
      ORDER BY source_job.updated_at DESC,source_job.job_id
      LIMIT 1
    ) AS job ON true
  ),
  classified AS (
    SELECT
      with_job.*,
      CASE
        WHEN content IS NULL OR btrim(content)=''
          THEN 'empty'
        WHEN content_sha256 IS NULL
          OR content_sha256 !~ '^[0-9a-f]{64}$'
          THEN 'skipped'
        WHEN job_status='completed' AND candidate_count=0
          THEN 'empty'
        WHEN job_status='completed' AND candidate_count>0
          THEN 'skipped'
        WHEN job_status='skipped'
          THEN 'skipped'
        WHEN job_status='review_required'
          THEN 'skipped'
        WHEN job_status IN ('pending','processing','error')
          THEN 'deferred'
        ELSE 'eligible'
      END AS planned_outcome,
      CASE
        WHEN content IS NULL OR btrim(content)=''
          THEN 'none'
        WHEN content_sha256 IS NULL
          OR content_sha256 !~ '^[0-9a-f]{64}$'
          THEN 'none'
        WHEN job_status IS NOT NULL
          THEN 'none'
        WHEN kind IN ('user_statement','external_observation')
          THEN 'relational_extraction'
        WHEN kind='document'
          THEN 'artifact_assessment'
        ELSE 'structured_projection'
      END AS planned_route,
      CASE
        WHEN content IS NULL OR btrim(content)=''
          THEN 'empty_content'
        WHEN content_sha256 IS NULL
          OR content_sha256 !~ '^[0-9a-f]{64}$'
          THEN 'missing_content_hash'
        WHEN job_status='completed' AND candidate_count=0
          THEN 'upstream_completed_empty'
        WHEN job_status='completed' AND candidate_count>0
          THEN 'upstream_link_inconsistency'
        WHEN job_status='skipped'
          THEN 'upstream_skipped'
        WHEN job_status='review_required'
          THEN 'upstream_review_required'
        WHEN job_status='pending'
          THEN 'upstream_pending'
        WHEN job_status='processing'
          THEN 'upstream_processing'
        WHEN job_status='error'
          THEN 'upstream_error'
        ELSE 'eligible_unprocessed'
      END AS planned_reason
    FROM with_job
  )
  SELECT
    classified.evidence_id,
    classified.content_sha256,
    classified.planned_outcome,
    classified.planned_route,
    classified.planned_reason,
    classified.job_id,
    classified.job_status::text,
    classified.pipeline_version,
    classified.candidate_count
  FROM classified
  ORDER BY classified.recorded_at,classified.evidence_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_owner_evidence_intake_terminal_v1(
  p_evidence_id uuid,
  p_selector_version text,
  p_expected_content_sha256 text,
  p_expected_outcome text,
  p_expected_reason_code text
)
RETURNS TABLE(
  terminal_id uuid,
  outcome text,
  apply_outcome text,
  decision_fingerprint text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  existing memory.evidence_intake_terminal%ROWTYPE;
  fingerprint text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'evidence intake recording requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_evidence_id IS NULL
     OR p_selector_version IS NULL
     OR p_selector_version !~ '^[a-z0-9][a-z0-9_.-]{2,63}$'
     OR p_expected_outcome NOT IN ('empty','skipped')
     OR btrim(COALESCE(p_expected_reason_code,''))='' THEN
    RAISE EXCEPTION 'evidence intake terminal inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,p_evidence_id::text,p_selector_version),
    0
  ));

  SELECT terminal.* INTO existing
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id=actor
    AND terminal.evidence_id=p_evidence_id
    AND terminal.selector_version=p_selector_version;
  IF FOUND THEN
    IF existing.evidence_content_sha256
         IS DISTINCT FROM p_expected_content_sha256
       OR existing.outcome IS DISTINCT FROM p_expected_outcome
       OR existing.reason_code IS DISTINCT FROM p_expected_reason_code THEN
      RAISE EXCEPTION 'evidence intake replay conflicts with terminal decision'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.terminal_id,
      existing.outcome,
      'replayed'::text,
      existing.decision_fingerprint;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_evidence_intake_v1(
    p_selector_version,1,p_evidence_id
  );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'evidence is not eligible for intake classification'
      USING ERRCODE='23514';
  END IF;
  IF planned.evidence_content_sha256
       IS DISTINCT FROM p_expected_content_sha256
     OR planned.outcome IS DISTINCT FROM p_expected_outcome
     OR planned.reason_code IS DISTINCT FROM p_expected_reason_code
     OR planned.outcome NOT IN ('empty','skipped') THEN
    RAISE EXCEPTION 'evidence intake plan changed or is not terminal'
      USING ERRCODE='23514';
  END IF;

  fingerprint := encode(
    public.digest(
      convert_to(
        jsonb_build_object(
          'owner_user_id',actor,
          'evidence_id',p_evidence_id,
          'selector_version',p_selector_version,
          'evidence_content_sha256',p_expected_content_sha256,
          'outcome',p_expected_outcome,
          'reason_code',p_expected_reason_code,
          'source_job_id',planned.source_job_id,
          'source_job_status',planned.source_job_status,
          'source_job_pipeline_version',
            planned.source_job_pipeline_version,
          'upstream_candidate_count',
            planned.upstream_candidate_count
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );

  INSERT INTO memory.evidence_intake_terminal(
    owner_user_id,
    evidence_id,
    selector_version,
    outcome,
    reason_code,
    evidence_content_sha256,
    source_job_id,
    source_job_status,
    source_job_pipeline_version,
    decision_fingerprint,
    actor_user_id,
    invoked_by_role,
    details
  ) VALUES (
    actor,
    p_evidence_id,
    p_selector_version,
    p_expected_outcome,
    p_expected_reason_code,
    p_expected_content_sha256,
    planned.source_job_id,
    planned.source_job_status::memory.consolidation_job_status,
    planned.source_job_pipeline_version,
    fingerprint,
    actor,
    session_user,
    jsonb_build_object(
      'route',planned.route,
      'upstream_candidate_count',planned.upstream_candidate_count
    )
  )
  RETURNING
    evidence_intake_terminal.terminal_id,
    evidence_intake_terminal.outcome,
    'applied'::text,
    evidence_intake_terminal.decision_fingerprint
  INTO terminal_id,outcome,apply_outcome,decision_fingerprint;

  RETURN NEXT;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_intake_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_intake_maintainer;
GRANT SELECT ON
  memory.evidence,
  memory.artifact_endorsement,
  memory.artifact_occurrence,
  memory.candidate,
  memory.claim_evidence,
  memory.entity_alias,
  memory.entity_mention,
  memory.evidence_ingest_batch_row,
  memory.observation_entailment_v5,
  memory.preference_candidate_evidence,
  memory.preference_revision_evidence,
  memory.project_knowledge_candidate_evidence,
  memory.project_knowledge_revision_evidence,
  memory.relational_stage_batch,
  memory.user_preference,
  memory.consolidation_job,
  memory.evidence_intake_terminal
TO memory_intake_maintainer;
GRANT INSERT ON memory.evidence_intake_terminal
  TO memory_intake_maintainer;

ALTER FUNCTION memory.plan_owner_evidence_intake_v1(text,integer,uuid)
  OWNER TO memory_intake_maintainer;
ALTER FUNCTION memory.record_owner_evidence_intake_terminal_v1(
  uuid,text,text,text,text
) OWNER TO memory_intake_maintainer;

REVOKE ALL ON memory.evidence_intake_terminal
  FROM PUBLIC,brains_app;
GRANT SELECT ON memory.evidence_intake_terminal TO brains_app;

REVOKE ALL ON FUNCTION
  memory.guard_evidence_intake_terminal_append_only()
  FROM PUBLIC,brains_app,memory_intake_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_evidence_intake_v1(text,integer,uuid)
  FROM PUBLIC,brains_app,memory_intake_maintainer;
REVOKE ALL ON FUNCTION
  memory.record_owner_evidence_intake_terminal_v1(
    uuid,text,text,text,text
  )
  FROM PUBLIC,brains_app,memory_intake_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_evidence_intake_v1(text,integer,uuid)
  TO brains_app,memory_intake_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.record_owner_evidence_intake_terminal_v1(
    uuid,text,text,text,text
  )
  TO brains_app;

COMMIT;
