BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local entailment migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.observation_entailment_v5') IS NULL
     OR to_regprocedure(
       'memory.preflight_observation_entailment_v5(uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_observation_entailment_v5(uuid,uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.guard_v5_local_inference_append_only()'
     ) IS NULL THEN
    RAISE EXCEPTION 'local entailment prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_entailment_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_local_entailment_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_entailment_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'local entailment maintainer role is overprivileged';
  END IF;
END
$role$;

CREATE TABLE IF NOT EXISTS memory.v5_local_entailment_assessment (
  assessment_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  stage_admission_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_sha256 text NOT NULL
    CHECK (observation_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_id uuid NOT NULL,
  evidence_content_sha256 text NOT NULL
    CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  model_sha256 text NOT NULL CHECK (model_sha256 ~ '^[0-9a-f]{64}$'),
  model_file_sha256 text NOT NULL
    CHECK (model_file_sha256 ~ '^[0-9a-f]{64}$'),
  runtime_revision_sha256 text NOT NULL
    CHECK (runtime_revision_sha256 ~ '^[0-9a-f]{64}$'),
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  response_sha256 text NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
  output_schema_sha256 text NOT NULL
    CHECK (output_schema_sha256 ~ '^[0-9a-f]{64}$'),
  raw_decision text NOT NULL
    CHECK (raw_decision IN ('entailed','contradicted','ambiguous')),
  confidence text NOT NULL CHECK (confidence IN ('high','medium','low')),
  governed_decision memory.observation_entailment_decision_v5 NOT NULL,
  reason_code text NOT NULL,
  source_spans jsonb NOT NULL,
  policy_version text NOT NULL
    CHECK (policy_version='memory_v1_v5_local_entailment_policy_v1'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,request_id),
  UNIQUE(owner_user_id,observation_id,policy_version),
  FOREIGN KEY(owner_user_id,stage_admission_id)
    REFERENCES memory.v5_local_packet_stage_admission(
      owner_user_id,admission_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,observation_id,observation_sha256)
    REFERENCES memory.observation(
      owner_user_id,observation_id,observation_sha256
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_source_spans_valid(source_spans)),
  CHECK (
    (raw_decision='entailed' AND confidence='high'
      AND governed_decision='accepted'
      AND reason_code='predicate_entailment_v5_1_accepted')
    OR
    (raw_decision='contradicted'
      AND governed_decision='deferred'
      AND reason_code='source_contradicts_predicate')
    OR
    ((raw_decision='ambiguous'
       OR (raw_decision='entailed' AND confidence<>'high'))
      AND governed_decision='deferred'
      AND reason_code='predicate_semantics_unresolved')
  )
);

CREATE INDEX IF NOT EXISTS v5_local_entailment_owner_time_idx
  ON memory.v5_local_entailment_assessment(
    owner_user_id,created_at,assessment_id
  );
ALTER TABLE memory.v5_local_entailment_assessment OWNER TO sage;
ALTER TABLE memory.v5_local_entailment_assessment ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_entailment_assessment FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_entailment_assessment;
CREATE POLICY owner_isolation
  ON memory.v5_local_entailment_assessment
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_entailment_assessment_append_only_guard
  ON memory.v5_local_entailment_assessment;
CREATE TRIGGER v5_local_entailment_assessment_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_entailment_assessment
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP POLICY IF EXISTS local_entailment_read
  ON memory.v5_local_packet_stage_admission;
CREATE POLICY local_entailment_read
  ON memory.v5_local_packet_stage_admission FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read ON memory.relational_stage_batch;
CREATE POLICY local_entailment_read
  ON memory.relational_stage_batch FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read ON memory.observation;
CREATE POLICY local_entailment_read
  ON memory.observation FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read ON memory.entity_mention;
CREATE POLICY local_entailment_read
  ON memory.entity_mention FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read
  ON memory.observation_entity_binding;
CREATE POLICY local_entailment_read
  ON memory.observation_entity_binding FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read ON memory.evidence;
CREATE POLICY local_entailment_read
  ON memory.evidence FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read
  ON memory.observation_entailment_v5;
CREATE POLICY local_entailment_read
  ON memory.observation_entailment_v5 FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_entailment_read
  ON memory.relational_operation_request;
CREATE POLICY local_entailment_read
  ON memory.relational_operation_request FOR SELECT
  TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());

GRANT SELECT ON
  memory.v5_local_packet_stage_admission,
  memory.relational_stage_batch,
  memory.observation,
  memory.entity_mention,
  memory.observation_entity_binding,
  memory.evidence,
  memory.observation_entailment_v5,
  memory.relational_operation_request
TO memory_v5_local_entailment_maintainer;
GRANT SELECT,INSERT ON memory.v5_local_entailment_assessment
TO memory_v5_local_entailment_maintainer;
GRANT USAGE ON SCHEMA memory TO memory_v5_local_entailment_maintainer;
GRANT USAGE ON TYPE memory.observation_entailment_decision_v5
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text)
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_source_spans_valid(jsonb)
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.preflight_observation_entailment_v5(
  uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
) TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.record_observation_entailment_v5(
  uuid,uuid,memory.observation_entailment_decision_v5,
  text,jsonb,text,text,text
) TO memory_v5_local_entailment_maintainer;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_entailment_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  stage_admission_id uuid,
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content text,
  evidence_content_sha256 text,
  source_spans jsonb,
  observation_payload jsonb,
  observation_created_at timestamptz
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
    RAISE EXCEPTION 'local entailment plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local entailment plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    stage.admission_id,o.observation_id,o.observation_sha256,
    evidence.evidence_id,evidence.content,evidence.content_sha256,
    o.source_spans,
    jsonb_build_object(
      'predicate',o.predicate,
      'polarity',o.polarity::text,
      'modality',o.modality::text,
      'projection_class',o.projection_class::text,
      'subject',jsonb_strip_nulls(jsonb_build_object(
        'entity_type',subject.entity_type,
        'mention_kind',subject.mention_kind::text,
        'name_text',subject.name_text,
        'relationship_role',subject.relationship_role
      )),
      'object',CASE
        WHEN object.mention_id IS NOT NULL THEN
          jsonb_strip_nulls(jsonb_build_object(
            'entity_type',object.entity_type,
            'mention_kind',object.mention_kind::text,
            'name_text',object.name_text,
            'relationship_role',object.relationship_role
          ))
        ELSE o.object_literal
      END,
      'source_spans',o.source_spans
    ),
    o.created_at
  FROM memory.v5_local_packet_stage_admission AS stage
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id
  JOIN memory.observation AS o
    ON o.owner_user_id=batch.owner_user_id
   AND o.evidence_id=batch.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=o.owner_user_id
   AND evidence.evidence_id=o.evidence_id
  JOIN memory.entity_mention AS subject
    ON subject.owner_user_id=o.owner_user_id
   AND subject.evidence_id=o.evidence_id
   AND subject.mention_id=o.subject_mention_id
  LEFT JOIN memory.entity_mention AS object
    ON object.owner_user_id=o.owner_user_id
   AND object.evidence_id=o.evidence_id
   AND object.mention_id=o.object_mention_id
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=o.owner_user_id
   AND binding.observation_id=o.observation_id
  WHERE stage.owner_user_id=actor
    AND stage.decision='auto_stage_eligible'
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
    AND o.projection_class<>'never_surface'
    AND NOT EXISTS (
      SELECT 1 FROM memory.observation_entailment_v5 AS prior
      WHERE prior.owner_user_id=actor
        AND prior.observation_id=o.observation_id
        AND prior.policy_version='memory_v1_predicate_entailment_v5_1'
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_entailment_assessment AS prior
      WHERE prior.owner_user_id=actor
        AND prior.observation_id=o.observation_id
        AND prior.policy_version='memory_v1_v5_local_entailment_policy_v1'
    )
  ORDER BY stage.created_at,o.created_at,o.observation_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_entailment_v1(
  p_assessment_id uuid,
  p_request_id uuid,
  p_stage_admission_id uuid,
  p_observation_id uuid,
  p_expected_observation_sha256 text,
  p_expected_evidence_content_sha256 text,
  p_model_sha256 text,
  p_model_file_sha256 text,
  p_runtime_revision_sha256 text,
  p_request_sha256 text,
  p_response_sha256 text,
  p_output_schema_sha256 text,
  p_raw_decision text,
  p_confidence text,
  p_governed_decision memory.observation_entailment_decision_v5,
  p_reason_code text,
  p_source_spans jsonb,
  p_policy_version text
)
RETURNS TABLE(
  assessment_id uuid,
  observation_id uuid,
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
  replayed memory.v5_local_entailment_assessment%ROWTYPE;
  preflight record;
  recorded record;
  assessor_ref constant text :=
    'memory_v1_v5_local_entailment_provider_v1';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local entailment register requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_assessment_id IS NULL OR p_request_id IS NULL
     OR p_stage_admission_id IS NULL OR p_observation_id IS NULL
     OR p_expected_observation_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_evidence_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_sha256 !~ '^[0-9a-f]{64}$'
     OR p_model_file_sha256 !~ '^[0-9a-f]{64}$'
     OR p_runtime_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_request_sha256 !~ '^[0-9a-f]{64}$'
     OR p_response_sha256 !~ '^[0-9a-f]{64}$'
     OR p_output_schema_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>'memory_v1_v5_local_entailment_policy_v1'
     OR NOT memory.v5_source_spans_valid(p_source_spans) THEN
    RAISE EXCEPTION 'local entailment inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  IF NOT (
    (p_raw_decision='entailed' AND p_confidence='high'
      AND p_governed_decision='accepted'
      AND p_reason_code='predicate_entailment_v5_1_accepted')
    OR
    (p_raw_decision='contradicted'
      AND p_confidence IN ('high','medium','low')
      AND p_governed_decision='deferred'
      AND p_reason_code='source_contradicts_predicate')
    OR
    ((p_raw_decision='ambiguous'
       OR (p_raw_decision='entailed' AND p_confidence IN ('medium','low')))
      AND p_governed_decision='deferred'
      AND p_reason_code='predicate_semantics_unresolved')
  ) THEN
    RAISE EXCEPTION 'local entailment decision mapping is invalid'
      USING ERRCODE='23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_entailment',actor::text,p_observation_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_entailment_assessment AS value
  WHERE value.owner_user_id=actor
    AND (value.assessment_id=p_assessment_id
      OR value.request_id=p_request_id
      OR (value.observation_id=p_observation_id
          AND value.policy_version=p_policy_version));
  IF FOUND THEN
    IF replayed.assessment_id<>p_assessment_id
       OR replayed.request_id<>p_request_id
       OR replayed.stage_admission_id<>p_stage_admission_id
       OR replayed.observation_sha256<>p_expected_observation_sha256
       OR replayed.evidence_content_sha256
            <>p_expected_evidence_content_sha256
       OR replayed.model_sha256<>p_model_sha256
       OR replayed.model_file_sha256<>p_model_file_sha256
       OR replayed.runtime_revision_sha256<>p_runtime_revision_sha256
       OR replayed.request_sha256<>p_request_sha256
       OR replayed.response_sha256<>p_response_sha256
       OR replayed.output_schema_sha256<>p_output_schema_sha256
       OR replayed.raw_decision<>p_raw_decision
       OR replayed.confidence<>p_confidence
       OR replayed.governed_decision<>p_governed_decision
       OR replayed.reason_code<>p_reason_code
       OR replayed.source_spans<>p_source_spans
       OR replayed.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local entailment replay conflicts'
        USING ERRCODE='23514';
    END IF;
    SELECT * INTO recorded
    FROM memory.record_observation_entailment_v5(
      p_request_id,p_observation_id,p_governed_decision,p_reason_code,
      p_source_spans,'system',assessor_ref,
      (SELECT authorization_manifest_sha256
       FROM memory.preflight_observation_entailment_v5(
         p_observation_id,p_governed_decision,p_reason_code,p_source_spans,
         'system',assessor_ref
       ))
    );
    IF recorded.outcome<>'replayed' OR recorded.rows_written<>0 THEN
      RAISE EXCEPTION 'local entailment replay invariant failed';
    END IF;
    RETURN QUERY SELECT replayed.assessment_id,replayed.observation_id,
      replayed.governed_decision::text,'replayed'::text,0;
    RETURN;
  END IF;

  SELECT
    stage.admission_id AS source_stage_admission_id,
    stage.decision AS stage_decision,
    o.observation_id AS source_observation_id,
    o.observation_sha256 AS source_observation_sha256,
    o.evidence_id,o.source_spans AS observation_source_spans,
    evidence.content_sha256,evidence.status::text AS evidence_status,
    binding.observation_id AS bound_observation_id
  INTO source
  FROM memory.v5_local_packet_stage_admission AS stage
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id
  JOIN memory.observation AS o
    ON o.owner_user_id=batch.owner_user_id
   AND o.evidence_id=batch.evidence_id
   AND o.observation_id=p_observation_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=o.owner_user_id
   AND evidence.evidence_id=o.evidence_id
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=o.owner_user_id
   AND binding.observation_id=o.observation_id
  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id;
  IF NOT FOUND OR source.stage_decision<>'auto_stage_eligible'
     OR source.evidence_status<>'active'
     OR source.source_observation_sha256<>p_expected_observation_sha256
     OR source.content_sha256<>p_expected_evidence_content_sha256
     OR source.observation_source_spans<>p_source_spans THEN
    RAISE EXCEPTION 'local entailment source is absent or changed'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.observation_entailment_v5 AS prior
    WHERE prior.owner_user_id=actor
      AND prior.observation_id=p_observation_id
      AND prior.policy_version='memory_v1_predicate_entailment_v5_1'
  ) THEN
    RAISE EXCEPTION 'observation already has an entailment decision'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_observation_entailment_v5(
    p_observation_id,p_governed_decision,p_reason_code,p_source_spans,
    'system',assessor_ref
  );
  SELECT * INTO recorded
  FROM memory.record_observation_entailment_v5(
    p_request_id,p_observation_id,p_governed_decision,p_reason_code,
    p_source_spans,'system',assessor_ref,
    preflight.authorization_manifest_sha256
  );
  IF recorded.outcome<>'applied' OR recorded.rows_written<>2 THEN
    RAISE EXCEPTION 'governed entailment record failed';
  END IF;

  INSERT INTO memory.v5_local_entailment_assessment(
    assessment_id,owner_user_id,request_id,stage_admission_id,
    observation_id,observation_sha256,evidence_id,
    evidence_content_sha256,model_sha256,model_file_sha256,
    runtime_revision_sha256,request_sha256,response_sha256,
    output_schema_sha256,raw_decision,confidence,governed_decision,
    reason_code,source_spans,policy_version
  ) VALUES (
    p_assessment_id,actor,p_request_id,p_stage_admission_id,
    p_observation_id,p_expected_observation_sha256,source.evidence_id,
    p_expected_evidence_content_sha256,p_model_sha256,p_model_file_sha256,
    p_runtime_revision_sha256,p_request_sha256,p_response_sha256,
    p_output_schema_sha256,p_raw_decision,p_confidence,
    p_governed_decision,p_reason_code,p_source_spans,p_policy_version
  );
  RETURN QUERY SELECT p_assessment_id,p_observation_id,
    p_governed_decision::text,'applied'::text,3;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_entailment_v1(integer)
  OWNER TO memory_v5_local_entailment_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
) OWNER TO memory_v5_local_entailment_maintainer;

REVOKE ALL ON memory.v5_local_entailment_assessment
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_entailment_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_entailment_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
) TO brains_app;

COMMIT;
