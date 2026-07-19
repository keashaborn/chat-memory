BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local claim projection migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_entailment_assessment') IS NULL
     OR to_regclass('memory.projection_plan') IS NULL
     OR to_regprocedure(
       'memory.stage_claim_projection_plan_v5_1(uuid,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local claim projection prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_local_projection_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_local_projection_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_projection_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'local projection maintainer is overprivileged';
  END IF;
END
$role$;

DO $assessment_constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.v5_local_entailment_assessment'::regclass
      AND conname='v5_local_entailment_assessment_owner_assessment_key'
  ) THEN
    ALTER TABLE memory.v5_local_entailment_assessment
      ADD CONSTRAINT v5_local_entailment_assessment_owner_assessment_key
      UNIQUE(owner_user_id,assessment_id);
  END IF;
END
$assessment_constraint$;

CREATE TABLE IF NOT EXISTS memory.v5_local_claim_projection_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  packet_sha256 text NOT NULL CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
  owner_manifest_sha256 text NOT NULL
    CHECK (owner_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  policy_version text NOT NULL
    CHECK (policy_version='memory_v1_v5_local_claim_projection_policy_v1'),
  decision text NOT NULL CHECK (decision='stage_manual_review_plan'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,assessment_id),
  UNIQUE(owner_user_id,observation_id),
  UNIQUE(owner_user_id,plan_id),
  FOREIGN KEY(owner_user_id,assessment_id)
    REFERENCES memory.v5_local_entailment_assessment(
      owner_user_id,assessment_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,observation_id)
    REFERENCES memory.observation(owner_user_id,observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,plan_id)
    REFERENCES memory.projection_plan(owner_user_id,plan_id)
    ON DELETE RESTRICT
);
ALTER TABLE memory.v5_local_claim_projection_admission OWNER TO sage;
ALTER TABLE memory.v5_local_claim_projection_admission ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_claim_projection_admission FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_claim_projection_admission;
CREATE POLICY owner_isolation
  ON memory.v5_local_claim_projection_admission
  TO memory_v5_local_projection_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_claim_projection_admission_append_only_guard
  ON memory.v5_local_claim_projection_admission;
CREATE TRIGGER v5_local_claim_projection_admission_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_claim_projection_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP POLICY IF EXISTS local_projection_read
  ON memory.v5_local_entailment_assessment;
CREATE POLICY local_projection_read
  ON memory.v5_local_entailment_assessment FOR SELECT
  TO memory_v5_local_projection_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_projection_read ON memory.observation;
CREATE POLICY local_projection_read
  ON memory.observation FOR SELECT
  TO memory_v5_local_projection_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_projection_read ON memory.projection_plan;
CREATE POLICY local_projection_read
  ON memory.projection_plan FOR SELECT
  TO memory_v5_local_projection_maintainer
  USING (owner_user_id=memory.current_actor_user_id());

GRANT USAGE ON SCHEMA memory TO memory_v5_local_projection_maintainer;
GRANT SELECT ON memory.v5_local_entailment_assessment,memory.observation,
  memory.projection_plan
TO memory_v5_local_projection_maintainer;
GRANT SELECT,INSERT ON memory.v5_local_claim_projection_admission
TO memory_v5_local_projection_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_v5_local_projection_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
TO memory_v5_local_projection_maintainer;
GRANT EXECUTE ON FUNCTION memory.stage_claim_projection_plan_v5_1(
  uuid,text,text
) TO memory_v5_local_projection_maintainer;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_claim_projection_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  assessment_id uuid,
  observation_id uuid,
  observation_sha256 text,
  predicate text,
  assessment_created_at timestamptz
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
    RAISE EXCEPTION 'local claim projection plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local claim projection plan limit is invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT assessment.assessment_id,assessment.observation_id,
    assessment.observation_sha256,observation.predicate,
    assessment.created_at
  FROM memory.v5_local_entailment_assessment AS assessment
  JOIN memory.observation AS observation
    ON observation.owner_user_id=assessment.owner_user_id
   AND observation.observation_id=assessment.observation_id
  WHERE assessment.owner_user_id=actor
    AND assessment.governed_decision='accepted'
    AND assessment.raw_decision='entailed'
    AND assessment.confidence='high'
    AND observation.predicate IN (
      'identity.name','pet.breed','pet.sex','relationship.has_pet'
    )
    AND observation.projection_class IN ('direct_claim','supportive_context')
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_claim_projection_v1(
  p_admission_id uuid,
  p_assessment_id uuid,
  p_observation_id uuid,
  p_plan_id uuid,
  p_packet_text text,
  p_expected_packet_sha256 text,
  p_expected_owner_manifest_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  plan_id uuid,
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
  assessment memory.v5_local_entailment_assessment%ROWTYPE;
  replayed memory.v5_local_claim_projection_admission%ROWTYPE;
  staged record;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local claim projection register requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_admission_id IS NULL OR p_assessment_id IS NULL
     OR p_observation_id IS NULL OR p_plan_id IS NULL
     OR p_packet_text IS NULL OR length(p_packet_text)>200000
     OR p_expected_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_owner_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>'memory_v1_v5_local_claim_projection_policy_v1'
     OR (p_packet_text::jsonb->>'packet_sha256')
          <>p_expected_packet_sha256 THEN
    RAISE EXCEPTION 'local claim projection inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_claim_projection',actor::text,
    p_observation_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_claim_projection_admission AS value
  WHERE value.owner_user_id=actor
    AND (value.admission_id=p_admission_id
      OR value.assessment_id=p_assessment_id
      OR value.observation_id=p_observation_id
      OR value.plan_id=p_plan_id);
  IF FOUND THEN
    IF replayed.admission_id<>p_admission_id
       OR replayed.assessment_id<>p_assessment_id
       OR replayed.observation_id<>p_observation_id
       OR replayed.plan_id<>p_plan_id
       OR replayed.packet_sha256<>p_expected_packet_sha256
       OR replayed.owner_manifest_sha256
            <>p_expected_owner_manifest_sha256
       OR replayed.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local claim projection replay conflicts'
        USING ERRCODE='23514';
    END IF;
    SELECT * INTO staged FROM memory.stage_claim_projection_plan_v5_1(
      p_plan_id,p_packet_text,p_expected_owner_manifest_sha256
    );
    IF staged.outcome<>'replayed' OR staged.rows_written<>0 THEN
      RAISE EXCEPTION 'local claim projection replay invariant failed';
    END IF;
    RETURN QUERY SELECT replayed.admission_id,replayed.plan_id,
      replayed.decision,'replayed'::text,0;
    RETURN;
  END IF;

  SELECT value.* INTO assessment
  FROM memory.v5_local_entailment_assessment AS value
  JOIN memory.observation AS observation
    ON observation.owner_user_id=value.owner_user_id
   AND observation.observation_id=value.observation_id
  WHERE value.owner_user_id=actor
    AND value.assessment_id=p_assessment_id
    AND value.observation_id=p_observation_id
    AND value.governed_decision='accepted'
    AND value.raw_decision='entailed'
    AND value.confidence='high'
    AND observation.predicate IN (
      'identity.name','pet.breed','pet.sex','relationship.has_pet'
    )
    AND observation.projection_class IN ('direct_claim','supportive_context');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'accepted local claim source not found'
      USING ERRCODE='P0002';
  END IF;

  SELECT * INTO staged FROM memory.stage_claim_projection_plan_v5_1(
    p_plan_id,p_packet_text,p_expected_owner_manifest_sha256
  );
  IF staged.outcome<>'applied' OR staged.rows_written<>4
     OR staged.result->>'packet_sha256'<>p_expected_packet_sha256 THEN
    RAISE EXCEPTION 'local claim projection stage failed';
  END IF;
  INSERT INTO memory.v5_local_claim_projection_admission(
    admission_id,owner_user_id,assessment_id,observation_id,plan_id,
    packet_sha256,owner_manifest_sha256,policy_version,decision
  ) VALUES (
    p_admission_id,actor,p_assessment_id,p_observation_id,p_plan_id,
    p_expected_packet_sha256,p_expected_owner_manifest_sha256,
    p_policy_version,'stage_manual_review_plan'
  );
  RETURN QUERY SELECT p_admission_id,p_plan_id,
    'stage_manual_review_plan'::text,'applied'::text,5;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_local_claim_projection_v1(integer)
  OWNER TO memory_v5_local_projection_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_claim_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_local_projection_maintainer;
REVOKE ALL ON memory.v5_local_claim_projection_admission
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_claim_projection_v1(integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_claim_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_claim_projection_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_claim_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) TO brains_app;

COMMIT;
