BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local auto-resolution migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.entity_resolution_plan') IS NULL
     OR to_regprocedure(
       'memory.preflight_entity_resolution_apply_v5(uuid,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'
     ) IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL THEN
    RAISE EXCEPTION 'local auto-resolution prerequisites are absent';
  END IF;
END
$preflight$;

DO $constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.v5_local_packet_stage_admission'::regclass
      AND conname='v5_local_packet_stage_admission_owner_admission_key'
  ) THEN
    ALTER TABLE memory.v5_local_packet_stage_admission
      ADD CONSTRAINT v5_local_packet_stage_admission_owner_admission_key
      UNIQUE(owner_user_id,admission_id);
  END IF;
END
$constraint$;

CREATE TABLE IF NOT EXISTS memory.v5_local_auto_resolution_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  stage_admission_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  mention_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  selected_entity_id uuid NOT NULL,
  apply_request_id uuid NOT NULL,
  decision_sha256 text NOT NULL
    CHECK (decision_sha256 ~ '^[0-9a-f]{64}$'),
  apply_manifest_sha256 text NOT NULL
    CHECK (apply_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  policy_version text NOT NULL
    CHECK (policy_version='memory_v1_v5_local_auto_resolution_policy_v1'),
  decision text NOT NULL CHECK (decision='auto_apply_exact_existing_link'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,resolution_id),
  UNIQUE(owner_user_id,apply_request_id),
  FOREIGN KEY(owner_user_id,stage_admission_id)
    REFERENCES memory.v5_local_packet_stage_admission(owner_user_id,admission_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id,resolution_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,mention_id)
    REFERENCES memory.entity_mention(owner_user_id,mention_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,selected_entity_id)
    REFERENCES memory.entity(owner_user_id,entity_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS v5_local_auto_resolution_admission_owner_time_idx
  ON memory.v5_local_auto_resolution_admission(
    owner_user_id,created_at,admission_id
  );
ALTER TABLE memory.v5_local_auto_resolution_admission OWNER TO sage;
ALTER TABLE memory.v5_local_auto_resolution_admission ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_auto_resolution_admission FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_auto_resolution_admission;
CREATE POLICY owner_isolation
  ON memory.v5_local_auto_resolution_admission
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_auto_resolution_admission_append_only_guard
  ON memory.v5_local_auto_resolution_admission;
CREATE TRIGGER v5_local_auto_resolution_admission_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_auto_resolution_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.relational_operation_request;
CREATE POLICY local_auto_resolution_read
  ON memory.relational_operation_request FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_plan;
CREATE POLICY local_auto_resolution_read
  ON memory.entity_resolution_plan FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_mention;
CREATE POLICY local_auto_resolution_read
  ON memory.entity_mention FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_review;
CREATE POLICY local_auto_resolution_read
  ON memory.entity_resolution_review FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
DROP POLICY IF EXISTS local_auto_resolution_read
  ON memory.entity_resolution_apply;
CREATE POLICY local_auto_resolution_read
  ON memory.entity_resolution_apply FOR SELECT
  TO memory_v5_local_disposition_maintainer
  USING (owner_user_id=memory.current_actor_user_id());

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_local_auto_resolution_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  stage_admission_id uuid,
  resolution_id uuid,
  mention_id uuid,
  evidence_id uuid,
  selected_entity_id uuid,
  decision_sha256 text,
  stage_bundle_sha256 text,
  resolution_created_at timestamptz
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
    RAISE EXCEPTION 'local auto-resolution plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'local auto-resolution plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    stage.admission_id,plan.resolution_id,plan.mention_id,plan.evidence_id,
    plan.selected_entity_id,plan.decision_sha256,stage.stage_bundle_sha256,
    plan.created_at
  FROM memory.v5_local_packet_stage_admission AS stage
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id
  JOIN memory.relational_operation_request AS stage_request
    ON stage_request.owner_user_id=stage.owner_user_id
   AND stage_request.request_id=stage.stage_request_id
   AND stage_request.operation='stage_packet'
   AND stage_request.outcome='applied'
  JOIN memory.entity_resolution_plan AS plan
    ON plan.owner_user_id=stage.owner_user_id
   AND plan.evidence_id=stage.evidence_id
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.mention_id=plan.mention_id
   AND mention.evidence_id=plan.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=plan.owner_user_id
   AND evidence.evidence_id=plan.evidence_id
  JOIN memory.entity AS selected
    ON selected.owner_user_id=plan.owner_user_id
   AND selected.entity_id=plan.selected_entity_id
  WHERE stage.owner_user_id=actor
    AND stage.decision='auto_stage_eligible'
    AND plan.action='link_existing'
    AND plan.decision_state='auto_link_eligible'
    AND plan.selected_entity_id IS NOT NULL
    AND (plan.proposed_entity IS NULL OR plan.proposed_entity='null'::jsonb)
    AND evidence.status='active'
    AND selected.status='active'
    AND NOT EXISTS (
      SELECT 1 FROM memory.entity_resolution_review AS review
      WHERE review.owner_user_id=actor
        AND review.resolution_id=plan.resolution_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.entity_resolution_apply AS applied
      WHERE applied.owner_user_id=actor
        AND applied.resolution_id=plan.resolution_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_auto_resolution_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.resolution_id=plan.resolution_id
    )
  ORDER BY stage.created_at,plan.created_at,plan.resolution_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_auto_resolution_v1(
  p_operation_id uuid,
  p_admission_id uuid,
  p_stage_admission_id uuid,
  p_resolution_id uuid,
  p_apply_request_id uuid,
  p_expected_selected_entity_id uuid,
  p_expected_decision_sha256 text,
  p_expected_apply_manifest_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  resolution_id uuid,
  selected_entity_id uuid,
  apply_request_id uuid,
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
  replayed memory.v5_local_auto_resolution_admission%ROWTYPE;
  preflight record;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local auto-resolution admission requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_admission_id IS NULL
     OR p_stage_admission_id IS NULL OR p_resolution_id IS NULL
     OR p_apply_request_id IS NULL OR p_expected_selected_entity_id IS NULL
     OR p_expected_decision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_apply_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>'memory_v1_v5_local_auto_resolution_policy_v1' THEN
    RAISE EXCEPTION 'local auto-resolution admission inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_v1_v5_local_auto_resolution',actor::text,
      p_resolution_id::text),0
  ));
  SELECT value.* INTO replayed
  FROM memory.v5_local_auto_resolution_admission AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id
      OR value.admission_id=p_admission_id
      OR value.resolution_id=p_resolution_id
      OR value.apply_request_id=p_apply_request_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.admission_id<>p_admission_id
       OR replayed.stage_admission_id<>p_stage_admission_id
       OR replayed.resolution_id<>p_resolution_id
       OR replayed.apply_request_id<>p_apply_request_id
       OR replayed.selected_entity_id<>p_expected_selected_entity_id
       OR replayed.decision_sha256<>p_expected_decision_sha256
       OR replayed.apply_manifest_sha256<>p_expected_apply_manifest_sha256
       OR replayed.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local auto-resolution admission replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.admission_id,replayed.resolution_id,
      replayed.selected_entity_id,replayed.apply_request_id,
      replayed.decision,'replayed'::text;
    RETURN;
  END IF;

  SELECT
    stage.admission_id AS source_stage_admission_id,
    stage.stage_bundle_sha256,stage.decision AS stage_decision,
    plan.*,mention.evidence_id AS mention_evidence_id,
    evidence.status::text AS evidence_status,
    selected.status::text AS selected_status
  INTO source
  FROM memory.v5_local_packet_stage_admission AS stage
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id
  JOIN memory.relational_operation_request AS stage_request
    ON stage_request.owner_user_id=stage.owner_user_id
   AND stage_request.request_id=stage.stage_request_id
   AND stage_request.operation='stage_packet'
   AND stage_request.outcome='applied'
  JOIN memory.entity_resolution_plan AS plan
    ON plan.owner_user_id=stage.owner_user_id
   AND plan.evidence_id=stage.evidence_id
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.mention_id=plan.mention_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=plan.owner_user_id
   AND evidence.evidence_id=plan.evidence_id
  JOIN memory.entity AS selected
    ON selected.owner_user_id=plan.owner_user_id
   AND selected.entity_id=plan.selected_entity_id
  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id
    AND plan.resolution_id=p_resolution_id;
  IF NOT FOUND
     OR source.stage_decision<>'auto_stage_eligible'
     OR source.action<>'link_existing'
     OR source.decision_state<>'auto_link_eligible'
     OR source.selected_entity_id IS DISTINCT FROM p_expected_selected_entity_id
     OR (source.proposed_entity IS NOT NULL
       AND source.proposed_entity<>'null'::jsonb)
     OR source.decision_sha256<>p_expected_decision_sha256
     OR source.mention_evidence_id<>source.evidence_id
     OR source.evidence_status<>'active' OR source.selected_status<>'active'
     OR EXISTS (
       SELECT 1 FROM memory.entity_resolution_review AS review
       WHERE review.owner_user_id=actor
         AND review.resolution_id=p_resolution_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.entity_resolution_apply AS applied
       WHERE applied.owner_user_id=actor
         AND applied.resolution_id=p_resolution_id
     ) THEN
    RAISE EXCEPTION 'local resolution is not exact-link auto-apply eligible'
      USING ERRCODE='23514';
  END IF;

  SELECT value.* INTO preflight
  FROM memory.preflight_entity_resolution_apply_v5(p_resolution_id,NULL) AS value;
  IF NOT FOUND OR preflight.action<>'link_existing'
     OR preflight.decision_state<>'auto_link_eligible'
     OR preflight.review_id IS NOT NULL
     OR preflight.prospective_entity_id IS DISTINCT FROM p_expected_selected_entity_id
     OR preflight.apply_manifest_sha256<>p_expected_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'local auto-resolution preflight drifted'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_auto_resolution_admission(
    admission_id,owner_user_id,operation_id,stage_admission_id,resolution_id,
    mention_id,evidence_id,selected_entity_id,apply_request_id,
    decision_sha256,apply_manifest_sha256,policy_version,decision
  ) VALUES (
    p_admission_id,actor,p_operation_id,p_stage_admission_id,p_resolution_id,
    source.mention_id,source.evidence_id,p_expected_selected_entity_id,
    p_apply_request_id,p_expected_decision_sha256,
    p_expected_apply_manifest_sha256,p_policy_version,
    'auto_apply_exact_existing_link'
  );
  RETURN QUERY SELECT p_admission_id,p_resolution_id,
    p_expected_selected_entity_id,p_apply_request_id,
    'auto_apply_exact_existing_link'::text,'applied'::text;
END
$function$;

GRANT SELECT,INSERT ON memory.v5_local_auto_resolution_admission
  TO memory_v5_local_disposition_maintainer;
GRANT SELECT ON
  memory.relational_stage_batch,
  memory.relational_operation_request,
  memory.entity_resolution_plan,
  memory.entity_mention,
  memory.evidence,
  memory.entity,
  memory.entity_resolution_review,
  memory.entity_resolution_apply
  TO memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_apply_v5(
  uuid,uuid
) TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_auto_resolution_v1(integer)
  OWNER TO memory_v5_local_disposition_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_auto_resolution_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON memory.v5_local_auto_resolution_admission
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_local_auto_resolution_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.register_owner_v5_local_auto_resolution_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_local_auto_resolution_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_local_auto_resolution_v1(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) TO brains_app;

COMMIT;
