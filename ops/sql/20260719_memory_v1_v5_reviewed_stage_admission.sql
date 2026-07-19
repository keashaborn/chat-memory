BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'reviewed-stage admission migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.relational_operation_request') IS NULL
     OR to_regclass('memory.entity_resolution_plan') IS NULL
     OR to_regclass('memory.entity_resolution_review') IS NULL
     OR to_regclass('memory.entity_resolution_apply') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regprocedure('memory.guard_v5_local_inference_append_only()') IS NULL
     OR to_regprocedure('memory.plan_owner_v5_local_entailment_v1(integer)') IS NULL
     OR to_regprocedure(
       'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'reviewed-stage admission prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_reviewed_stage_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_reviewed_stage_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_reviewed_stage_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'reviewed-stage maintainer is overprivileged';
  END IF;
END
$role$;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_policy_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_decision_check
  CHECK (
    (decision='auto_stage_eligible'
      AND policy_version='memory_v1_v5_local_auto_stage_policy_v1')
    OR
    (decision='validated_entity_stage'
      AND policy_version='memory_v1_v5_local_entity_validation_policy_v1')
    OR
    (decision='reviewed_entity_stage'
      AND policy_version='memory_v1_v5_reviewed_stage_admission_policy_v1')
  );

CREATE TABLE IF NOT EXISTS memory.v5_local_reviewed_stage_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  stage_manifest_sha256 text NOT NULL
    CHECK (stage_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  resolution_state_sha256 text NOT NULL
    CHECK (resolution_state_sha256 ~ '^[0-9a-f]{64}$'),
  policy_version text NOT NULL CHECK (
    policy_version='memory_v1_v5_reviewed_stage_admission_policy_v1'
  ),
  decision text NOT NULL CHECK (decision='reviewed_entity_stage'),
  resolution_count smallint NOT NULL
    CHECK (resolution_count BETWEEN 1 AND 24),
  approved_review_count smallint NOT NULL
    CHECK (approved_review_count BETWEEN 1 AND 24),
  binding_count smallint NOT NULL
    CHECK (binding_count BETWEEN 1 AND 32),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,admission_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,artifact_id),
  UNIQUE(owner_user_id,batch_id),
  FOREIGN KEY(owner_user_id,admission_id)
    REFERENCES memory.v5_local_packet_stage_admission(
      owner_user_id,admission_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,artifact_id)
    REFERENCES memory.v5_local_packet_review_artifact(
      owner_user_id,artifact_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,batch_id)
    REFERENCES memory.relational_stage_batch(owner_user_id,batch_id)
    ON DELETE RESTRICT
);
ALTER TABLE memory.v5_local_reviewed_stage_admission
  OWNER TO memory_v5_reviewed_stage_maintainer;
ALTER TABLE memory.v5_local_reviewed_stage_admission
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_reviewed_stage_admission
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_reviewed_stage_admission;
CREATE POLICY owner_isolation
  ON memory.v5_local_reviewed_stage_admission
  TO memory_v5_reviewed_stage_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_reviewed_stage_append_only_guard
  ON memory.v5_local_reviewed_stage_admission;
CREATE TRIGGER v5_local_reviewed_stage_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_reviewed_stage_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP POLICY IF EXISTS reviewed_stage_access
  ON memory.v5_local_packet_stage_admission;
CREATE POLICY reviewed_stage_access
  ON memory.v5_local_packet_stage_admission
  TO memory_v5_reviewed_stage_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DO $read_policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_local_packet_review_artifact'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.relational_operation_request'::regclass,
    'memory.entity_resolution_plan'::regclass,
    'memory.entity_resolution_review'::regclass,
    'memory.entity_resolution_apply'::regclass,
    'memory.observation_entity_binding'::regclass
  ] LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS reviewed_stage_read ON %s',target
    );
    EXECUTE format(
      'CREATE POLICY reviewed_stage_read ON %s FOR SELECT TO memory_v5_reviewed_stage_maintainer USING (owner_user_id=memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$read_policies$;

GRANT USAGE ON SCHEMA memory TO memory_v5_reviewed_stage_maintainer;
GRANT SELECT ON
  memory.v5_local_packet_review_artifact,
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,
  memory.evidence,
  memory.relational_stage_batch,
  memory.relational_operation_request,
  memory.entity_resolution_plan,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.observation_entity_binding
TO memory_v5_reviewed_stage_maintainer;
GRANT SELECT,INSERT ON memory.v5_local_packet_stage_admission,
  memory.v5_local_reviewed_stage_admission
TO memory_v5_reviewed_stage_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_v5_reviewed_stage_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text)
TO memory_v5_reviewed_stage_maintainer;

CREATE OR REPLACE FUNCTION memory.v5_reviewed_stage_source_v1(
  p_artifact_id uuid
)
RETURNS TABLE(
  artifact_id uuid,
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  batch_id uuid,
  stage_request_id uuid,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  packet_storage_sha256 text,
  repository_commit text,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  entity_mention_count integer,
  observation_count integer,
  resolution_count integer,
  approved_review_count integer,
  binding_count integer,
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
    RAISE EXCEPTION 'reviewed-stage source requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;

  RETURN QUERY
  WITH source AS (
    SELECT
      artifact.artifact_id,
      artifact.packet_id,
      artifact.job_id,
      artifact.evidence_id,
      batch.batch_id,
      artifact.request_id AS stage_request_id,
      artifact.review_report_sha256,
      artifact.stage_bundle_sha256,
      artifact.packet_storage_sha256,
      artifact.repository_commit,
      batch.stage_manifest_sha256,
      packet.entity_mention_count,
      packet.observation_count,
      batch.resolution_count,
      artifact.created_at AS artifact_created_at,
      batch.result
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
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=artifact.owner_user_id
     AND request.request_id=artifact.request_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=artifact.owner_user_id
     AND batch.batch_id=(request.result->>'batch_id')::uuid
     AND batch.evidence_id=artifact.evidence_id
    WHERE artifact.owner_user_id=actor
      AND artifact.artifact_id=p_artifact_id
      AND artifact.review_disposition='manual_review_required'
      AND artifact.manual_review_count>=1
      AND artifact.deferred_count=0
      AND artifact.rejected_count=0
      AND packet.provider_id='local_llama_cpp'
      AND packet.local_model_calls=1
      AND packet.external_model_calls=0
      AND packet.manual_review_required
      AND packet.packet_storage_sha256=artifact.packet_storage_sha256
      AND packet.validator_packet_sha256=batch.extraction_packet_sha256
      AND packet.evidence_content_sha256=evidence.content_sha256
      AND packet.entity_mention_count=batch.mention_count
      AND packet.observation_count=batch.observation_count
      AND batch.resolution_count=batch.mention_count
      AND batch.temporal_count=batch.observation_count
      AND batch.extractor='memory_v1_v5_local_packet_review'
      AND batch.extractor_version=artifact.repository_commit
      AND batch.stage_manifest_sha256=request.manifest_sha256
      AND job.status='review_required'
      AND job.route='relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status='active'
      AND jsonb_typeof(batch.result->'resolution_ids')='object'
      AND jsonb_typeof(batch.result->'observation_ids')='object'
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'resolution_ids'))
            =batch.resolution_count
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'observation_ids'))
            =batch.observation_count
  ), exact_resolutions AS (
    SELECT source.*,
      plan.resolution_id,plan.action,plan.decision_state,
      applied.applied_entity_id,applied.apply_manifest_sha256,
      review.review_id,review.authorization_manifest_sha256
    FROM source
    JOIN LATERAL jsonb_each_text(source.result->'resolution_ids') AS ids
      ON true
    JOIN memory.entity_resolution_plan AS plan
      ON plan.owner_user_id=actor
     AND plan.resolution_id=ids.value::uuid
     AND plan.evidence_id=source.evidence_id
    LEFT JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id=actor
     AND applied.resolution_id=plan.resolution_id
    LEFT JOIN memory.entity_resolution_review AS review
      ON review.owner_user_id=actor
     AND review.resolution_id=plan.resolution_id
     AND review.decision='approved'
  ), resolution_state AS (
    SELECT
      exact_resolutions.artifact_id,
      count(*)::integer AS exact_resolution_count,
      count(*) FILTER (WHERE applied_entity_id IS NOT NULL)::integer
        AS applied_count,
      count(*) FILTER (WHERE decision_state='manual_review_required')::integer
        AS manual_count,
      count(*) FILTER (
        WHERE decision_state='manual_review_required'
          AND review_id IS NOT NULL
      )::integer AS approved_review_count,
      count(*) FILTER (
        WHERE action NOT IN ('link_existing','create_new')
           OR decision_state NOT IN (
             'auto_link_eligible','manual_review_required'
           )
      )::integer AS invalid_resolution_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        resolution_id::text,action::text,decision_state::text,
        coalesce(applied_entity_id::text,''),
        coalesce(apply_manifest_sha256,''),
        coalesce(review_id::text,''),
        coalesce(authorization_manifest_sha256,'')
      ),E'\n' ORDER BY resolution_id),'')) AS resolution_state_sha256
    FROM exact_resolutions
    GROUP BY exact_resolutions.artifact_id
  ), binding_state AS (
    SELECT source.artifact_id,count(binding.observation_id)::integer
      AS binding_count
    FROM source
    JOIN LATERAL jsonb_each_text(source.result->'observation_ids') AS ids
      ON true
    LEFT JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=actor
     AND binding.observation_id=ids.value::uuid
    GROUP BY source.artifact_id
  )
  SELECT source.artifact_id,source.packet_id,source.job_id,
    source.evidence_id,source.batch_id,source.stage_request_id,
    source.review_report_sha256,source.stage_bundle_sha256,
    source.packet_storage_sha256,source.repository_commit,
    source.stage_manifest_sha256,
    resolution_state.resolution_state_sha256,
    source.entity_mention_count,source.observation_count,
    source.resolution_count,resolution_state.approved_review_count,
    binding_state.binding_count,source.artifact_created_at
  FROM source
  JOIN resolution_state
    ON resolution_state.artifact_id=source.artifact_id
  JOIN binding_state
    ON binding_state.artifact_id=source.artifact_id
  WHERE resolution_state.exact_resolution_count=source.resolution_count
    AND resolution_state.applied_count=source.resolution_count
    AND resolution_state.manual_count>=1
    AND resolution_state.approved_review_count=resolution_state.manual_count
    AND resolution_state.invalid_resolution_count=0
    AND binding_state.binding_count=source.observation_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_reviewed_stage_admission_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  artifact_id uuid,
  packet_id uuid,
  batch_id uuid,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  entity_mention_count integer,
  observation_count integer,
  resolution_count integer,
  approved_review_count integer,
  binding_count integer,
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
    RAISE EXCEPTION 'reviewed-stage plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'reviewed-stage limit is invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT source.artifact_id,source.packet_id,source.batch_id,
    source.stage_manifest_sha256,source.resolution_state_sha256,
    source.entity_mention_count,source.observation_count,
    source.resolution_count,source.approved_review_count,
    source.binding_count,source.artifact_created_at
  FROM memory.v5_local_packet_review_artifact AS artifact
  CROSS JOIN LATERAL memory.v5_reviewed_stage_source_v1(
    artifact.artifact_id
  ) AS source
  WHERE artifact.owner_user_id=actor
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_stage_admission AS admission
      WHERE admission.owner_user_id=actor
        AND (admission.artifact_id=source.artifact_id
          OR admission.packet_id=source.packet_id)
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_reviewed_stage_admission AS admission
      WHERE admission.owner_user_id=actor
        AND (admission.artifact_id=source.artifact_id
          OR admission.batch_id=source.batch_id)
    )
  ORDER BY source.artifact_created_at,source.artifact_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_reviewed_stage_admission_v1(
  p_operation_id uuid,
  p_admission_id uuid,
  p_artifact_id uuid,
  p_expected_batch_id uuid,
  p_expected_stage_manifest_sha256 text,
  p_expected_resolution_state_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  artifact_id uuid,
  batch_id uuid,
  decision text,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  source record;
  replayed memory.v5_local_reviewed_stage_admission%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-stage register requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_admission_id IS NULL
     OR p_artifact_id IS NULL OR p_expected_batch_id IS NULL
     OR p_expected_stage_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_resolution_state_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>
          'memory_v1_v5_reviewed_stage_admission_policy_v1' THEN
    RAISE EXCEPTION 'reviewed-stage inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_reviewed_stage_admission',actor::text,
    p_artifact_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_reviewed_stage_admission AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id
      OR value.admission_id=p_admission_id
      OR value.artifact_id=p_artifact_id
      OR value.batch_id=p_expected_batch_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.admission_id<>p_admission_id
       OR replayed.artifact_id<>p_artifact_id
       OR replayed.batch_id<>p_expected_batch_id
       OR replayed.stage_manifest_sha256
            <>p_expected_stage_manifest_sha256
       OR replayed.resolution_state_sha256
            <>p_expected_resolution_state_sha256
       OR replayed.policy_version<>p_policy_version
       OR replayed.decision<>'reviewed_entity_stage'
       OR NOT EXISTS (
         SELECT 1 FROM memory.v5_local_packet_stage_admission AS stage
         WHERE stage.owner_user_id=actor
           AND stage.admission_id=replayed.admission_id
           AND stage.artifact_id=replayed.artifact_id
           AND stage.decision='reviewed_entity_stage'
           AND stage.policy_version=p_policy_version
       ) THEN
      RAISE EXCEPTION 'reviewed-stage replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.admission_id,replayed.artifact_id,
      replayed.batch_id,replayed.decision,'replayed'::text,0;
    RETURN;
  END IF;

  SELECT * INTO source
  FROM memory.v5_reviewed_stage_source_v1(p_artifact_id);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'eligible reviewed-stage source not found'
      USING ERRCODE='P0002';
  END IF;
  IF source.batch_id<>p_expected_batch_id
     OR source.stage_manifest_sha256
          <>p_expected_stage_manifest_sha256
     OR source.resolution_state_sha256
          <>p_expected_resolution_state_sha256 THEN
    RAISE EXCEPTION 'reviewed-stage source drifted'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_packet_stage_admission AS stage
    WHERE stage.owner_user_id=actor
      AND (stage.artifact_id=source.artifact_id
        OR stage.packet_id=source.packet_id
        OR stage.stage_request_id=source.stage_request_id)
  ) THEN
    RAISE EXCEPTION 'packet already has a different stage admission'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_stage_admission(
    admission_id,owner_user_id,operation_id,artifact_id,packet_id,
    job_id,evidence_id,stage_request_id,review_report_sha256,
    stage_bundle_sha256,packet_storage_sha256,repository_commit,
    policy_version,decision,entity_mention_count,observation_count
  ) VALUES (
    p_admission_id,actor,p_operation_id,source.artifact_id,
    source.packet_id,source.job_id,source.evidence_id,
    source.stage_request_id,source.review_report_sha256,
    source.stage_bundle_sha256,source.packet_storage_sha256,
    source.repository_commit,p_policy_version,'reviewed_entity_stage',
    source.entity_mention_count,source.observation_count
  );
  INSERT INTO memory.v5_local_reviewed_stage_admission(
    admission_id,owner_user_id,operation_id,artifact_id,batch_id,
    stage_manifest_sha256,resolution_state_sha256,policy_version,
    decision,resolution_count,approved_review_count,binding_count
  ) VALUES (
    p_admission_id,actor,p_operation_id,source.artifact_id,
    source.batch_id,source.stage_manifest_sha256,
    source.resolution_state_sha256,p_policy_version,
    'reviewed_entity_stage',source.resolution_count,
    source.approved_review_count,source.binding_count
  );
  RETURN QUERY SELECT p_admission_id,source.artifact_id,source.batch_id,
    'reviewed_entity_stage'::text,'applied'::text,2;
END
$function$;

ALTER FUNCTION memory.v5_reviewed_stage_source_v1(uuid)
  OWNER TO memory_v5_reviewed_stage_maintainer;
ALTER FUNCTION memory.plan_owner_v5_reviewed_stage_admission_v1(integer)
  OWNER TO memory_v5_reviewed_stage_maintainer;
ALTER FUNCTION memory.register_owner_v5_reviewed_stage_admission_v1(
  uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_reviewed_stage_maintainer;
REVOKE ALL ON FUNCTION memory.v5_reviewed_stage_source_v1(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_reviewed_stage_admission_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.register_owner_v5_reviewed_stage_admission_v1(
  uuid,uuid,uuid,uuid,text,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_reviewed_stage_admission_v1(integer)
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.register_owner_v5_reviewed_stage_admission_v1(
    uuid,uuid,uuid,uuid,text,text,text
  )
TO brains_app;

DO $entailment_compatibility$
DECLARE
  plan_def text;
  register_def text;
  prior_plan constant text :=
    'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'')';
  next_plan constant text :=
    'stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'')';
  prior_register constant text :=
    'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'')';
  next_register constant text :=
    'source.stage_decision NOT IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'')';
BEGIN
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(next_plan IN plan_def)=0 THEN
    IF position(prior_plan IN plan_def)=0 THEN
      RAISE EXCEPTION 'local entailment planner definition drifted';
    END IF;
    EXECUTE replace(plan_def,prior_plan,next_plan);
  END IF;
  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(next_register IN register_def)=0 THEN
    IF position(prior_register IN register_def)=0 THEN
      RAISE EXCEPTION 'local entailment register definition drifted';
    END IF;
    EXECUTE replace(register_def,prior_register,next_register);
  END IF;
END
$entailment_compatibility$;

COMMIT;
