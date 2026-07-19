BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local entity validation migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.entity_resolution_plan') IS NULL
     OR to_regprocedure('memory.stage_relational_packet_v5(uuid,uuid,text,text,text,text,text,text)') IS NULL
     OR to_regprocedure('memory.review_entity_resolution_v5(uuid,uuid,memory.entity_review_decision,text,text)') IS NULL
     OR to_regprocedure('memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)') IS NULL THEN
    RAISE EXCEPTION 'local entity validation prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_entity_validation_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_local_entity_validation_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_entity_validation_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'local entity validation maintainer is overprivileged';
  END IF;
END
$role$;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS v5_local_packet_stage_admission_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS v5_local_packet_stage_admission_policy_version_check;
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
  );

CREATE TABLE IF NOT EXISTS memory.v5_local_entity_validation_assessment (
  assessment_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  entity_ref text NOT NULL CHECK (entity_ref ~ '^e[0-9]{2}$'),
  mention_sha256 text NOT NULL CHECK (mention_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_content_sha256 text NOT NULL
    CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  stage_bundle_sha256 text NOT NULL
    CHECK (stage_bundle_sha256 ~ '^[0-9a-f]{64}$'),
  provider_model_sha256 text NOT NULL
    CHECK (provider_model_sha256 ~ '^[0-9a-f]{64}$'),
  model_file_sha256 text NOT NULL
    CHECK (model_file_sha256 ~ '^[0-9a-f]{64}$'),
  runtime_revision_sha256 text NOT NULL
    CHECK (runtime_revision_sha256 ~ '^[0-9a-f]{64}$'),
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  response_sha256 text NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
  output_schema_sha256 text NOT NULL
    CHECK (output_schema_sha256 ~ '^[0-9a-f]{64}$'),
  raw_decision text NOT NULL
    CHECK (raw_decision IN ('supported','contradicted','ambiguous')),
  confidence text NOT NULL CHECK (confidence IN ('high','medium','low')),
  governed_decision text NOT NULL
    CHECK (governed_decision IN ('accepted','deferred')),
  reason_code text NOT NULL CHECK (reason_code IN (
    'explicit_named_entity_supported',
    'source_contradicts_named_entity',
    'named_entity_support_unresolved'
  )),
  source_spans jsonb NOT NULL CHECK (
    jsonb_typeof(source_spans)='array' AND jsonb_array_length(source_spans)>0
  ),
  policy_version text NOT NULL CHECK (
    policy_version='memory_v1_v5_local_entity_validation_policy_v1'
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,assessment_id),
  UNIQUE(owner_user_id,artifact_id,entity_ref),
  UNIQUE(owner_user_id,packet_id,entity_ref),
  FOREIGN KEY(owner_user_id,artifact_id)
    REFERENCES memory.v5_local_packet_review_artifact(owner_user_id,artifact_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(owner_user_id,packet_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (
    (raw_decision='supported' AND confidence='high'
      AND governed_decision='accepted'
      AND reason_code='explicit_named_entity_supported')
    OR
    (governed_decision='deferred' AND (
      (raw_decision='contradicted'
       AND reason_code='source_contradicts_named_entity')
      OR
      (raw_decision<>'contradicted'
       AND NOT (raw_decision='supported' AND confidence='high')
       AND reason_code='named_entity_support_unresolved')
    ))
  )
);

CREATE TABLE IF NOT EXISTS memory.v5_local_validated_stage_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  stage_bundle_sha256 text NOT NULL
    CHECK (stage_bundle_sha256 ~ '^[0-9a-f]{64}$'),
  policy_version text NOT NULL CHECK (
    policy_version='memory_v1_v5_local_entity_validation_policy_v1'
  ),
  decision text NOT NULL CHECK (
    decision='stage_and_review_validated_new_entity'
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,admission_id),
  UNIQUE(owner_user_id,assessment_id),
  UNIQUE(owner_user_id,artifact_id),
  UNIQUE(owner_user_id,batch_id),
  UNIQUE(owner_user_id,resolution_id),
  FOREIGN KEY(owner_user_id,assessment_id)
    REFERENCES memory.v5_local_entity_validation_assessment(
      owner_user_id,assessment_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,artifact_id)
    REFERENCES memory.v5_local_packet_review_artifact(owner_user_id,artifact_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,batch_id)
    REFERENCES memory.relational_stage_batch(owner_user_id,batch_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id,resolution_id)
    ON DELETE RESTRICT
);

ALTER TABLE memory.v5_local_entity_validation_assessment
  OWNER TO memory_v5_local_entity_validation_maintainer;
ALTER TABLE memory.v5_local_validated_stage_admission
  OWNER TO memory_v5_local_entity_validation_maintainer;
ALTER TABLE memory.v5_local_entity_validation_assessment
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_entity_validation_assessment
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_validated_stage_admission
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_validated_stage_admission
  FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_entity_validation_assessment;
CREATE POLICY owner_isolation
  ON memory.v5_local_entity_validation_assessment
  TO memory_v5_local_entity_validation_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_validated_stage_admission;
CREATE POLICY owner_isolation
  ON memory.v5_local_validated_stage_admission
  TO memory_v5_local_entity_validation_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DROP TRIGGER IF EXISTS v5_local_entity_validation_append_only_guard
  ON memory.v5_local_entity_validation_assessment;
CREATE TRIGGER v5_local_entity_validation_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_entity_validation_assessment
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();
DROP TRIGGER IF EXISTS v5_local_validated_stage_append_only_guard
  ON memory.v5_local_validated_stage_admission;
CREATE TRIGGER v5_local_validated_stage_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_validated_stage_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DO $policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_local_packet_review_artifact'::regclass,
    'memory.v5_local_packet_stage_admission'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.entity_mention'::regclass,
    'memory.entity_resolution_plan'::regclass
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS local_entity_validation_read ON %s',target);
    EXECUTE format(
      'CREATE POLICY local_entity_validation_read ON %s FOR SELECT TO memory_v5_local_entity_validation_maintainer USING (owner_user_id=memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$policies$;

GRANT USAGE ON SCHEMA memory
TO memory_v5_local_entity_validation_maintainer;
GRANT SELECT ON memory.v5_local_packet_review_artifact,
  memory.v5_local_packet_stage_admission,
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,memory.evidence,
  memory.relational_stage_batch,memory.entity_mention,
  memory.entity_resolution_plan
TO memory_v5_local_entity_validation_maintainer;
GRANT INSERT ON memory.v5_local_packet_stage_admission
TO memory_v5_local_entity_validation_maintainer;
GRANT SELECT,INSERT ON memory.v5_local_entity_validation_assessment,
  memory.v5_local_validated_stage_admission
TO memory_v5_local_entity_validation_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_v5_local_entity_validation_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text)
TO memory_v5_local_entity_validation_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
TO memory_v5_local_entity_validation_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_canonical_json_text(jsonb)
TO memory_v5_local_entity_validation_maintainer;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_entity_validation_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  artifact_id uuid,
  packet_id uuid,
  evidence_id uuid,
  assessment_id uuid,
  route text,
  packet_storage_sha256 text,
  review_report_sha256 text,
  stage_bundle_sha256 text,
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
    RAISE EXCEPTION 'local entity validation plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local entity validation limit is invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT artifact.artifact_id,artifact.packet_id,artifact.evidence_id,
    assessment.assessment_id,
    CASE WHEN assessment.assessment_id IS NULL
      THEN 'validate_new_entity' ELSE 'stage_validated_entity' END,
    artifact.packet_storage_sha256,artifact.review_report_sha256,
    artifact.stage_bundle_sha256,
    artifact.created_at
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
  LEFT JOIN memory.v5_local_entity_validation_assessment AS assessment
    ON assessment.owner_user_id=artifact.owner_user_id
   AND assessment.artifact_id=artifact.artifact_id
   AND assessment.governed_decision='accepted'
  WHERE artifact.owner_user_id=actor
    AND artifact.manual_review_count=1
    AND artifact.deferred_count=0
    AND artifact.rejected_count=0
    AND artifact.blocking_code_count=0
    AND packet.manual_review_required
    AND job.status='review_required'
    AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=artifact.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_entity_validation_assessment AS rejected
      WHERE rejected.owner_user_id=actor
        AND rejected.artifact_id=artifact.artifact_id
        AND rejected.governed_decision='deferred'
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_validated_stage_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.artifact_id=artifact.artifact_id
    )
  ORDER BY CASE WHEN assessment.assessment_id IS NULL THEN 1 ELSE 0 END,
    artifact.created_at,artifact.artifact_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_entity_validation_v1(
  p_assessment_id uuid,
  p_artifact_id uuid,
  p_packet_id uuid,
  p_evidence_id uuid,
  p_entity_ref text,
  p_mention_sha256 text,
  p_evidence_content_sha256 text,
  p_packet_storage_sha256 text,
  p_stage_bundle_sha256 text,
  p_provider_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_request_sha256 text,
  p_response_sha256 text,
  p_output_schema_sha256 text,
  p_raw_decision text,
  p_confidence text,
  p_governed_decision text,
  p_reason_code text,
  p_source_spans jsonb,
  p_policy_version text
)
RETURNS TABLE(
  assessment_id uuid,
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
  artifact memory.v5_local_packet_review_artifact%ROWTYPE;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  mention jsonb;
  existing memory.v5_local_entity_validation_assessment%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local entity validation register requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_assessment_id IS NULL OR p_artifact_id IS NULL OR p_packet_id IS NULL
     OR p_evidence_id IS NULL OR p_entity_ref !~ '^e[0-9]{2}$'
     OR NOT memory.v5_sha256_valid(p_mention_sha256)
     OR NOT memory.v5_sha256_valid(p_evidence_content_sha256)
     OR NOT memory.v5_sha256_valid(p_packet_storage_sha256)
     OR NOT memory.v5_sha256_valid(p_stage_bundle_sha256)
     OR NOT memory.v5_sha256_valid(p_provider_model_sha256)
     OR NOT memory.v5_sha256_valid(p_model_file_sha256)
     OR NOT memory.v5_sha256_valid(p_runtime_revision_sha256)
     OR NOT memory.v5_sha256_valid(p_request_sha256)
     OR NOT memory.v5_sha256_valid(p_response_sha256)
     OR NOT memory.v5_sha256_valid(p_output_schema_sha256)
     OR p_policy_version<>'memory_v1_v5_local_entity_validation_policy_v1'
     OR jsonb_typeof(p_source_spans)<>'array'
     OR jsonb_array_length(p_source_spans)=0 THEN
    RAISE EXCEPTION 'local entity validation inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_entity_validation',actor::text,
    p_artifact_id::text,p_entity_ref
  ),0));

  SELECT value.* INTO existing
  FROM memory.v5_local_entity_validation_assessment AS value
  WHERE value.owner_user_id=actor
    AND (value.assessment_id=p_assessment_id
      OR (value.artifact_id=p_artifact_id AND value.entity_ref=p_entity_ref));
  IF FOUND THEN
    IF existing.assessment_id<>p_assessment_id
       OR existing.packet_id<>p_packet_id
       OR existing.evidence_id<>p_evidence_id
       OR existing.mention_sha256<>p_mention_sha256
       OR existing.stage_bundle_sha256<>p_stage_bundle_sha256
       OR existing.request_sha256<>p_request_sha256
       OR existing.response_sha256<>p_response_sha256
       OR existing.raw_decision<>p_raw_decision
       OR existing.confidence<>p_confidence
       OR existing.governed_decision<>p_governed_decision
       OR existing.reason_code<>p_reason_code
       OR existing.source_spans<>p_source_spans
       OR existing.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local entity validation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing.assessment_id,
      existing.governed_decision,'replayed'::text,0;
    RETURN;
  END IF;

  SELECT value.* INTO STRICT artifact
  FROM memory.v5_local_packet_review_artifact AS value
  WHERE value.owner_user_id=actor AND value.artifact_id=p_artifact_id
    AND value.packet_id=p_packet_id AND value.evidence_id=p_evidence_id
    AND value.packet_storage_sha256=p_packet_storage_sha256
    AND value.stage_bundle_sha256=p_stage_bundle_sha256
    AND value.manual_review_count=1 AND value.deferred_count=0
    AND value.rejected_count=0 AND value.blocking_code_count=0;
  SELECT value.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor AND value.packet_id=p_packet_id
    AND value.evidence_id=p_evidence_id
    AND value.packet_storage_sha256=p_packet_storage_sha256
    AND value.manual_review_required;
  SELECT value.* INTO STRICT evidence
  FROM memory.evidence AS value
  WHERE value.owner_user_id=actor AND value.evidence_id=p_evidence_id
    AND value.status='active'
    AND value.content_sha256=p_evidence_content_sha256
    AND memory.v5_digest_text(value.content)=value.content_sha256;
  SELECT value INTO STRICT mention
  FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions') AS value
  WHERE value->>'entity_ref'=p_entity_ref
    AND value->>'mention_kind'='named'
    AND value->>'entity_type' NOT IN ('self','project')
    AND btrim(value->>'name_text')<>''
    AND value->'source_spans'=p_source_spans;
  IF memory.v5_digest_text(memory.v5_canonical_json_text(mention))
       <>p_mention_sha256 THEN
    RAISE EXCEPTION 'local entity validation mention hash mismatch'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.relational_stage_batch AS stage
    WHERE stage.owner_user_id=actor AND stage.evidence_id=p_evidence_id
  ) THEN
    RAISE EXCEPTION 'local entity validation source was already staged'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_entity_validation_assessment(
    assessment_id,owner_user_id,artifact_id,packet_id,evidence_id,
    entity_ref,mention_sha256,evidence_content_sha256,
    packet_storage_sha256,stage_bundle_sha256,provider_model_sha256,
    model_file_sha256,runtime_revision_sha256,request_sha256,
    response_sha256,output_schema_sha256,raw_decision,confidence,
    governed_decision,reason_code,source_spans,policy_version
  ) VALUES (
    p_assessment_id,actor,p_artifact_id,p_packet_id,p_evidence_id,
    p_entity_ref,p_mention_sha256,p_evidence_content_sha256,
    p_packet_storage_sha256,p_stage_bundle_sha256,p_provider_model_sha256,
    p_model_file_sha256,p_runtime_revision_sha256,p_request_sha256,
    p_response_sha256,p_output_schema_sha256,p_raw_decision,p_confidence,
    p_governed_decision,p_reason_code,p_source_spans,p_policy_version
  );
  RETURN QUERY SELECT p_assessment_id,p_governed_decision,'applied'::text,1;
END
$function$;

DROP FUNCTION IF EXISTS memory.register_owner_v5_local_validated_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
);
CREATE FUNCTION memory.register_owner_v5_local_validated_stage_v1(
  p_admission_id uuid,
  p_operation_id uuid,
  p_assessment_id uuid,
  p_artifact_id uuid,
  p_batch_id uuid,
  p_expected_stage_bundle_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  resolution_id uuid,
  auto_resolution_ids jsonb,
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
  assessment memory.v5_local_entity_validation_assessment%ROWTYPE;
  stage memory.relational_stage_batch%ROWTYPE;
  resolution memory.entity_resolution_plan%ROWTYPE;
  existing memory.v5_local_validated_stage_admission%ROWTYPE;
  auto_ids jsonb;
  expected_auto_count integer;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'validated local stage register requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_admission_id IS NULL OR p_operation_id IS NULL
     OR p_assessment_id IS NULL
     OR p_artifact_id IS NULL OR p_batch_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_stage_bundle_sha256)
     OR p_policy_version<>'memory_v1_v5_local_entity_validation_policy_v1' THEN
    RAISE EXCEPTION 'validated local stage inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_validated_stage',actor::text,p_artifact_id::text
  ),0));
  SELECT value.* INTO existing
  FROM memory.v5_local_validated_stage_admission AS value
  WHERE value.owner_user_id=actor
    AND (value.admission_id=p_admission_id
      OR value.assessment_id=p_assessment_id
      OR value.artifact_id=p_artifact_id
      OR value.batch_id=p_batch_id);
  IF FOUND THEN
    IF existing.admission_id<>p_admission_id
       OR existing.assessment_id<>p_assessment_id
       OR existing.artifact_id<>p_artifact_id
       OR existing.batch_id<>p_batch_id
       OR existing.stage_bundle_sha256<>p_expected_stage_bundle_sha256
       OR existing.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'validated local stage replay conflicts'
        USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_stage_admission AS source
      WHERE source.owner_user_id=actor
        AND source.admission_id=p_admission_id
        AND source.operation_id=p_operation_id
        AND source.artifact_id=p_artifact_id
        AND source.policy_version=p_policy_version
        AND source.decision='validated_entity_stage'
    ) THEN
      RAISE EXCEPTION 'validated source-stage admission replay is incomplete'
        USING ERRCODE='23514';
    END IF;
    SELECT COALESCE(jsonb_agg(plan.resolution_id ORDER BY plan.resolution_id),
      '[]'::jsonb) INTO auto_ids
    FROM memory.entity_resolution_plan AS plan
    JOIN memory.entity_mention AS mention
      ON mention.owner_user_id=plan.owner_user_id
     AND mention.evidence_id=plan.evidence_id
     AND mention.mention_id=plan.mention_id
    JOIN memory.relational_stage_batch AS replay_stage
      ON replay_stage.owner_user_id=plan.owner_user_id
     AND replay_stage.batch_id=existing.batch_id
     AND replay_stage.evidence_id=plan.evidence_id
    WHERE plan.owner_user_id=actor
      AND mention.packet_sha256=replay_stage.extraction_packet_sha256
      AND plan.action='link_existing'
      AND plan.decision_state='auto_link_eligible';
    RETURN QUERY SELECT existing.admission_id,existing.resolution_id,
      auto_ids,'replayed'::text,0;
    RETURN;
  END IF;
  SELECT value.* INTO STRICT assessment
  FROM memory.v5_local_entity_validation_assessment AS value
  WHERE value.owner_user_id=actor
    AND value.assessment_id=p_assessment_id
    AND value.artifact_id=p_artifact_id
    AND value.stage_bundle_sha256=p_expected_stage_bundle_sha256
    AND value.governed_decision='accepted';
  SELECT value.* INTO STRICT stage
  FROM memory.relational_stage_batch AS value
  WHERE value.owner_user_id=actor AND value.batch_id=p_batch_id
    AND value.evidence_id=assessment.evidence_id;
  SELECT plan.* INTO STRICT resolution
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.evidence_id=plan.evidence_id
   AND mention.mention_id=plan.mention_id
  WHERE plan.owner_user_id=actor
    AND plan.evidence_id=assessment.evidence_id
    AND mention.packet_sha256=stage.extraction_packet_sha256
    AND mention.entity_ref=assessment.entity_ref
    AND mention.mention_sha256=assessment.mention_sha256
    AND plan.action='create_new'
    AND plan.decision_state='manual_review_required';
  SELECT artifact.auto_link_count INTO STRICT expected_auto_count
  FROM memory.v5_local_packet_review_artifact AS artifact
  WHERE artifact.owner_user_id=actor
    AND artifact.artifact_id=assessment.artifact_id;
  SELECT COALESCE(jsonb_agg(plan.resolution_id ORDER BY plan.resolution_id),
    '[]'::jsonb) INTO auto_ids
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.evidence_id=plan.evidence_id
   AND mention.mention_id=plan.mention_id
  WHERE plan.owner_user_id=actor
    AND plan.evidence_id=assessment.evidence_id
    AND mention.packet_sha256=stage.extraction_packet_sha256
    AND plan.action='link_existing'
    AND plan.decision_state='auto_link_eligible';
  IF jsonb_array_length(auto_ids)<>expected_auto_count THEN
    RAISE EXCEPTION 'validated local stage auto-resolution count drifted'
      USING ERRCODE='23514';
  END IF;
  INSERT INTO memory.v5_local_packet_stage_admission(
    admission_id,owner_user_id,operation_id,artifact_id,packet_id,
    job_id,evidence_id,stage_request_id,review_report_sha256,
    stage_bundle_sha256,packet_storage_sha256,repository_commit,
    policy_version,decision,entity_mention_count,observation_count
  )
  SELECT p_admission_id,actor,p_operation_id,artifact.artifact_id,
    artifact.packet_id,artifact.job_id,artifact.evidence_id,
    artifact.request_id,artifact.review_report_sha256,
    artifact.stage_bundle_sha256,artifact.packet_storage_sha256,
    artifact.repository_commit,p_policy_version,'validated_entity_stage',
    packet.entity_mention_count,packet.observation_count
  FROM memory.v5_local_packet_review_artifact AS artifact
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=artifact.owner_user_id
   AND packet.packet_id=artifact.packet_id
  WHERE artifact.owner_user_id=actor
    AND artifact.artifact_id=assessment.artifact_id;
  INSERT INTO memory.v5_local_validated_stage_admission(
    admission_id,owner_user_id,assessment_id,artifact_id,batch_id,
    resolution_id,stage_bundle_sha256,policy_version,decision
  ) VALUES (
    p_admission_id,actor,p_assessment_id,p_artifact_id,p_batch_id,
    resolution.resolution_id,p_expected_stage_bundle_sha256,
    p_policy_version,'stage_and_review_validated_new_entity'
  );
  RETURN QUERY SELECT p_admission_id,resolution.resolution_id,auto_ids,
    'applied'::text,1;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_entity_validation_v1(integer)
  OWNER TO memory_v5_local_entity_validation_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_entity_validation_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,
  text,text,text,text,jsonb,text
) OWNER TO memory_v5_local_entity_validation_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_validated_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
) OWNER TO memory_v5_local_entity_validation_maintainer;

REVOKE ALL ON memory.v5_local_entity_validation_assessment,
  memory.v5_local_validated_stage_admission FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_entity_validation_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_entity_validation_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,
  text,text,text,text,jsonb,text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_validated_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_entity_validation_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_entity_validation_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,
  text,text,text,text,jsonb,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_validated_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text
) TO brains_app;

COMMIT;
