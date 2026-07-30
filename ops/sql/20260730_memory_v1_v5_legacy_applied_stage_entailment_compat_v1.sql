BEGIN;

DO $prerequisites$
BEGIN
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regprocedure(
       'memory.v5_local_stage_admission_batch_v2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_entailment_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.register_owner_v5_local_entailment_v1(
         uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
         memory.observation_entailment_decision_v5,text,jsonb,text
       )'
     ) IS NULL THEN
    RAISE EXCEPTION
      'legacy-stage entailment compatibility prerequisites are absent';
  END IF;
END
$prerequisites$;

DO $role$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'memory_v5_legacy_stage_compat_maintainer'
  ) THEN
    CREATE ROLE memory_v5_legacy_stage_compat_maintainer
      NOLOGIN NOINHERIT;
  END IF;
END
$role$;

ALTER TABLE memory.v5_local_packet_stage_admission
  ADD COLUMN IF NOT EXISTS source_legacy_stage_batch_id uuid,
  ADD COLUMN IF NOT EXISTS allowed_observation_set_sha256 text;

ALTER TABLE memory.v5_local_packet_stage_admission
  ALTER COLUMN packet_id DROP NOT NULL,
  ALTER COLUMN job_id DROP NOT NULL;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_allowed_observation_set_sha_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT
    v5_local_packet_stage_admission_allowed_observation_set_sha_check
  CHECK (
    allowed_observation_set_sha256 IS NULL
    OR allowed_observation_set_sha256 ~ '^[0-9a-f]{64}$'
  );

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_owner_legacy_batch_fkey;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT
    v5_local_packet_stage_admission_owner_legacy_batch_fkey
  FOREIGN KEY (owner_user_id, source_legacy_stage_batch_id)
  REFERENCES memory.relational_stage_batch(owner_user_id, batch_id)
  ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS
  v5_local_packet_stage_admission_owner_legacy_batch_key
ON memory.v5_local_packet_stage_admission(
  owner_user_id, source_legacy_stage_batch_id
)
WHERE source_legacy_stage_batch_id IS NOT NULL;

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT IF EXISTS
    v5_local_packet_stage_admission_policy_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_decision_check
  CHECK (
    (
      source_legacy_stage_batch_id IS NULL
      AND allowed_observation_set_sha256 IS NULL
      AND packet_id IS NOT NULL
      AND job_id IS NOT NULL
      AND (
        (
          artifact_id IS NOT NULL
          AND source_route_event_id IS NULL
          AND source_atom_apply_id IS NULL
          AND (
            (
              decision = 'auto_stage_eligible'
              AND policy_version =
                'memory_v1_v5_local_auto_stage_policy_v1'
            )
            OR (
              decision = 'validated_entity_stage'
              AND policy_version =
                'memory_v1_v5_local_entity_validation_policy_v1'
            )
            OR (
              decision = 'reviewed_entity_stage'
              AND policy_version =
                'memory_v1_v5_reviewed_stage_admission_policy_v1'
            )
          )
        )
        OR (
          artifact_id IS NULL
          AND source_route_event_id IS NOT NULL
          AND source_atom_apply_id IS NOT NULL
          AND decision = 'v5_2_atom_reviewed_stage'
          AND policy_version =
            'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
        )
        OR (
          artifact_id IS NULL
          AND source_route_event_id IS NOT NULL
          AND source_atom_apply_id IS NULL
          AND decision = 'v5_2_reviewed_route_stage'
          AND policy_version =
            'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
        )
      )
    )
    OR (
      source_legacy_stage_batch_id IS NOT NULL
      AND allowed_observation_set_sha256 IS NOT NULL
      AND artifact_id IS NULL
      AND packet_id IS NULL
      AND job_id IS NULL
      AND source_route_event_id IS NULL
      AND source_atom_apply_id IS NULL
      AND decision = 'legacy_applied_stage_entailment'
      AND policy_version =
        'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
    )
  );

CREATE TABLE IF NOT EXISTS
memory.v5_local_legacy_stage_admission_observation (
  owner_user_id uuid NOT NULL,
  admission_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_sha256 text NOT NULL
    CHECK (observation_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, admission_id, observation_id),
  FOREIGN KEY (owner_user_id, admission_id)
    REFERENCES memory.v5_local_packet_stage_admission(
      owner_user_id, admission_id
    )
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT
);
ALTER TABLE memory.v5_local_legacy_stage_admission_observation
  OWNER TO sage;
ALTER TABLE memory.v5_local_legacy_stage_admission_observation
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_legacy_stage_admission_observation
  FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS legacy_stage_compat_access
  ON memory.v5_local_legacy_stage_admission_observation;
CREATE POLICY legacy_stage_compat_access
  ON memory.v5_local_legacy_stage_admission_observation
  TO memory_v5_legacy_stage_compat_maintainer
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

DROP POLICY IF EXISTS local_entailment_read
  ON memory.v5_local_legacy_stage_admission_observation;
CREATE POLICY local_entailment_read
  ON memory.v5_local_legacy_stage_admission_observation
  FOR SELECT TO memory_v5_local_entailment_maintainer
  USING (owner_user_id = memory.current_actor_user_id());

DROP TRIGGER IF EXISTS
  v5_local_legacy_stage_admission_observation_append_only_guard
ON memory.v5_local_legacy_stage_admission_observation;
CREATE TRIGGER
  v5_local_legacy_stage_admission_observation_append_only_guard
BEFORE UPDATE OR DELETE
ON memory.v5_local_legacy_stage_admission_observation
FOR EACH ROW
EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DROP POLICY IF EXISTS legacy_stage_compat_access
  ON memory.v5_local_packet_stage_admission;
CREATE POLICY legacy_stage_compat_access
  ON memory.v5_local_packet_stage_admission
  TO memory_v5_legacy_stage_compat_maintainer
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

DO $source_read_policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.relational_stage_batch'::regclass,
    'memory.relational_operation_request'::regclass,
    'memory.evidence'::regclass,
    'memory.observation'::regclass,
    'memory.observation_entity_binding'::regclass,
    'memory.observation_entailment_v5'::regclass,
    'memory.v5_local_entailment_assessment'::regclass,
    'memory.claim_observation'::regclass
  ] LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS legacy_stage_compat_read ON %s',
      target
    );
    EXECUTE format(
      'CREATE POLICY legacy_stage_compat_read ON %s FOR SELECT TO memory_v5_legacy_stage_compat_maintainer USING (owner_user_id=memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$source_read_policies$;

GRANT USAGE ON SCHEMA memory
TO memory_v5_legacy_stage_compat_maintainer;
GRANT SELECT ON
  memory.relational_stage_batch,
  memory.relational_operation_request,
  memory.evidence,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_entailment_v5,
  memory.v5_local_entailment_assessment,
  memory.claim_observation
TO memory_v5_legacy_stage_compat_maintainer;
GRANT SELECT, INSERT ON
  memory.v5_local_packet_stage_admission,
  memory.v5_local_legacy_stage_admission_observation
TO memory_v5_legacy_stage_compat_maintainer;
GRANT SELECT ON
  memory.v5_local_legacy_stage_admission_observation
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.current_actor_user_id(),
  memory.v5_digest_text(text),
  memory.v5_sha256_valid(text)
TO memory_v5_legacy_stage_compat_maintainer;

CREATE OR REPLACE FUNCTION
memory.v5_local_legacy_stage_compat_source_v1(
  p_batch_id uuid,
  p_stage_request_id uuid
)
RETURNS TABLE(
  batch_id uuid,
  evidence_id uuid,
  stage_request_id uuid,
  evidence_content_sha256 text,
  stage_manifest_sha256 text,
  extraction_packet_sha256 text,
  resolution_packet_sha256 text,
  extractor text,
  extractor_version text,
  mention_count smallint,
  allowed_observation_count smallint,
  allowed_observation_set_sha256 text,
  allowed_observations jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'legacy-stage compatibility source requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;

  RETURN QUERY
  WITH source AS (
    SELECT
      batch.batch_id,
      batch.evidence_id,
      request.request_id AS stage_request_id,
      evidence.content_sha256 AS evidence_content_sha256,
      batch.stage_manifest_sha256,
      batch.extraction_packet_sha256,
      batch.resolution_packet_sha256,
      batch.extractor,
      batch.extractor_version,
      batch.mention_count::smallint AS mention_count
    FROM memory.relational_stage_batch AS batch
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id = batch.owner_user_id
     AND request.request_id = p_stage_request_id
     AND request.operation = 'stage_packet'
     AND request.outcome = 'applied'
     AND (request.result ->> 'batch_id')::uuid = batch.batch_id
     AND request.manifest_sha256 = batch.stage_manifest_sha256
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = batch.owner_user_id
     AND evidence.evidence_id = batch.evidence_id
     AND evidence.status = 'active'
     AND evidence.content IS NOT NULL
     AND memory.v5_digest_text(evidence.content) =
         evidence.content_sha256
    WHERE batch.owner_user_id = actor
      AND batch.batch_id = p_batch_id
  ),
  allowed AS (
    SELECT
      source.batch_id,
      source.evidence_id,
      source.stage_request_id,
      source.evidence_content_sha256,
      source.stage_manifest_sha256,
      source.extraction_packet_sha256,
      source.resolution_packet_sha256,
      source.extractor,
      source.extractor_version,
      source.mention_count,
      jsonb_agg(
        jsonb_build_object(
          'observation_id', observation.observation_id,
          'observation_sha256', observation.observation_sha256
        )
        ORDER BY observation.observation_id
      ) AS allowed_observations,
      count(*)::smallint AS allowed_observation_count
    FROM source
    JOIN memory.observation AS observation
      ON observation.owner_user_id = actor
     AND observation.evidence_id = source.evidence_id
     AND observation.projection_class <> 'never_surface'
    JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id = observation.owner_user_id
     AND binding.observation_id = observation.observation_id
    WHERE NOT EXISTS (
        SELECT 1
        FROM memory.observation_entailment_v5 AS prior
        WHERE prior.owner_user_id = observation.owner_user_id
          AND prior.observation_id = observation.observation_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_local_entailment_assessment AS prior
        WHERE prior.owner_user_id = observation.owner_user_id
          AND prior.observation_id = observation.observation_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.claim_observation AS linked
        WHERE linked.owner_user_id = observation.owner_user_id
          AND linked.observation_id = observation.observation_id
      )
    GROUP BY
      source.batch_id,
      source.evidence_id,
      source.stage_request_id,
      source.evidence_content_sha256,
      source.stage_manifest_sha256,
      source.extraction_packet_sha256,
      source.resolution_packet_sha256,
      source.extractor,
      source.extractor_version,
      source.mention_count
    HAVING count(*) BETWEEN 1 AND 32
  )
  SELECT
    allowed.batch_id,
    allowed.evidence_id,
    allowed.stage_request_id,
    allowed.evidence_content_sha256,
    allowed.stage_manifest_sha256,
    allowed.extraction_packet_sha256,
    allowed.resolution_packet_sha256,
    allowed.extractor,
    allowed.extractor_version,
    allowed.mention_count,
    allowed.allowed_observation_count,
    encode(
      public.digest(
        convert_to(allowed.allowed_observations::text, 'UTF8'),
        'sha256'
      ),
      'hex'
    ),
    allowed.allowed_observations
  FROM allowed;
END
$function$;

ALTER FUNCTION memory.v5_local_legacy_stage_compat_source_v1(uuid, uuid)
  OWNER TO memory_v5_legacy_stage_compat_maintainer;
REVOKE ALL ON FUNCTION
  memory.v5_local_legacy_stage_compat_source_v1(uuid, uuid)
FROM PUBLIC, brains_app;

CREATE OR REPLACE FUNCTION
memory.register_owner_v5_legacy_stage_compat_v1(
  p_operation_id uuid,
  p_admission_id uuid,
  p_batch_id uuid,
  p_expected_evidence_id uuid,
  p_expected_stage_request_id uuid,
  p_expected_evidence_content_sha256 text,
  p_expected_stage_manifest_sha256 text,
  p_expected_extraction_packet_sha256 text,
  p_expected_resolution_packet_sha256 text,
  p_expected_allowed_observation_set_sha256 text,
  p_expected_allowed_observation_count integer,
  p_review_report_sha256 text,
  p_repository_commit text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  batch_id uuid,
  allowed_observation_count integer,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  source record;
  replayed memory.v5_local_packet_stage_admission%ROWTYPE;
  replay_count integer;
  replay_hash text;
  inserted_members integer;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'legacy-stage compatibility registration requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_admission_id IS NULL
     OR p_batch_id IS NULL
     OR p_expected_evidence_id IS NULL
     OR p_expected_stage_request_id IS NULL
     OR NOT memory.v5_sha256_valid(
       p_expected_evidence_content_sha256
     )
     OR NOT memory.v5_sha256_valid(
       p_expected_stage_manifest_sha256
     )
     OR NOT memory.v5_sha256_valid(
       p_expected_extraction_packet_sha256
     )
     OR NOT memory.v5_sha256_valid(
       p_expected_resolution_packet_sha256
     )
     OR NOT memory.v5_sha256_valid(
       p_expected_allowed_observation_set_sha256
     )
     OR NOT memory.v5_sha256_valid(p_review_report_sha256)
     OR p_expected_allowed_observation_count NOT BETWEEN 1 AND 32
     OR p_repository_commit !~ '^[0-9a-f]{40}$'
     OR p_policy_version <>
       'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
  THEN
    RAISE EXCEPTION
      'legacy-stage compatibility inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      concat_ws(
        '|',
        'memory_v1_v5_legacy_stage_compat',
        actor::text,
        p_batch_id::text
      ),
      0
    )
  );

  SELECT value.*
  INTO replayed
  FROM memory.v5_local_packet_stage_admission AS value
  WHERE value.owner_user_id = actor
    AND (
      value.admission_id = p_admission_id
      OR value.operation_id = p_operation_id
      OR value.source_legacy_stage_batch_id = p_batch_id
      OR value.stage_request_id = p_expected_stage_request_id
    );

  IF FOUND THEN
    SELECT
      count(*)::integer,
      encode(
        public.digest(
          convert_to(
            jsonb_agg(
              jsonb_build_object(
                'observation_id', member.observation_id,
                'observation_sha256', member.observation_sha256
              )
              ORDER BY member.observation_id
            )::text,
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
    INTO replay_count, replay_hash
    FROM memory.v5_local_legacy_stage_admission_observation AS member
    WHERE member.owner_user_id = actor
      AND member.admission_id = replayed.admission_id;

    IF replayed.admission_id <> p_admission_id
       OR replayed.operation_id <> p_operation_id
       OR replayed.source_legacy_stage_batch_id <> p_batch_id
       OR replayed.evidence_id <> p_expected_evidence_id
       OR replayed.stage_request_id <> p_expected_stage_request_id
       OR replayed.stage_bundle_sha256 <>
          p_expected_stage_manifest_sha256
       OR replayed.packet_storage_sha256 <>
          p_expected_extraction_packet_sha256
       OR replayed.allowed_observation_set_sha256 <>
          p_expected_allowed_observation_set_sha256
       OR replayed.review_report_sha256 <> p_review_report_sha256
       OR replayed.repository_commit <> p_repository_commit
       OR replayed.policy_version <> p_policy_version
       OR replayed.decision <> 'legacy_applied_stage_entailment'
       OR replay_count <> p_expected_allowed_observation_count
       OR replay_hash <> p_expected_allowed_observation_set_sha256
    THEN
      RAISE EXCEPTION
        'legacy-stage compatibility replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
    SELECT
      replayed.admission_id,
      replayed.source_legacy_stage_batch_id,
      replay_count,
      'replayed'::text,
      0;
    RETURN;
  END IF;

  SELECT *
  INTO source
  FROM memory.v5_local_legacy_stage_compat_source_v1(
    p_batch_id,
    p_expected_stage_request_id
  );
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'eligible legacy-stage compatibility source not found'
      USING ERRCODE = 'P0002';
  END IF;
  IF source.evidence_id <> p_expected_evidence_id
     OR source.evidence_content_sha256 <>
        p_expected_evidence_content_sha256
     OR source.stage_manifest_sha256 <>
        p_expected_stage_manifest_sha256
     OR source.extraction_packet_sha256 <>
        p_expected_extraction_packet_sha256
     OR source.resolution_packet_sha256 <>
        p_expected_resolution_packet_sha256
     OR source.allowed_observation_set_sha256 <>
        p_expected_allowed_observation_set_sha256
     OR source.allowed_observation_count <>
        p_expected_allowed_observation_count
  THEN
    RAISE EXCEPTION
      'legacy-stage compatibility source drifted'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO memory.v5_local_packet_stage_admission(
    admission_id,
    owner_user_id,
    operation_id,
    artifact_id,
    packet_id,
    job_id,
    evidence_id,
    stage_request_id,
    review_report_sha256,
    stage_bundle_sha256,
    packet_storage_sha256,
    repository_commit,
    policy_version,
    decision,
    entity_mention_count,
    observation_count,
    source_route_event_id,
    source_atom_apply_id,
    source_legacy_stage_batch_id,
    allowed_observation_set_sha256
  ) VALUES (
    p_admission_id,
    actor,
    p_operation_id,
    NULL,
    NULL,
    NULL,
    source.evidence_id,
    source.stage_request_id,
    p_review_report_sha256,
    source.stage_manifest_sha256,
    source.extraction_packet_sha256,
    p_repository_commit,
    p_policy_version,
    'legacy_applied_stage_entailment',
    source.mention_count,
    source.allowed_observation_count,
    NULL,
    NULL,
    source.batch_id,
    source.allowed_observation_set_sha256
  );

  INSERT INTO memory.v5_local_legacy_stage_admission_observation(
    owner_user_id,
    admission_id,
    observation_id,
    observation_sha256
  )
  SELECT
    actor,
    p_admission_id,
    (item ->> 'observation_id')::uuid,
    item ->> 'observation_sha256'
  FROM jsonb_array_elements(source.allowed_observations) AS item;
  GET DIAGNOSTICS inserted_members = ROW_COUNT;
  IF inserted_members <> source.allowed_observation_count THEN
    RAISE EXCEPTION
      'legacy-stage compatibility member count drifted';
  END IF;

  RETURN QUERY
  SELECT
    p_admission_id,
    source.batch_id,
    inserted_members,
    'applied'::text,
    1 + inserted_members;
END
$function$;

ALTER FUNCTION memory.register_owner_v5_legacy_stage_compat_v1(
  uuid,uuid,uuid,uuid,uuid,text,text,text,text,text,integer,text,text,text
) OWNER TO memory_v5_legacy_stage_compat_maintainer;
REVOKE ALL ON FUNCTION
  memory.register_owner_v5_legacy_stage_compat_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text,text,integer,text,text,text
  )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.register_owner_v5_legacy_stage_compat_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text,text,integer,text,text,text
  )
TO brains_app;

CREATE OR REPLACE FUNCTION
memory.v5_local_stage_observation_allowed_v1(
  p_stage_admission_id uuid,
  p_observation_id uuid,
  p_observation_sha256 text
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  stage_decision text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'stage observation authorization requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;

  SELECT stage.decision
  INTO stage_decision
  FROM memory.v5_local_packet_stage_admission AS stage
  WHERE stage.owner_user_id = actor
    AND stage.admission_id = p_stage_admission_id;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  IF stage_decision <> 'legacy_applied_stage_entailment' THEN
    RETURN true;
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM memory.v5_local_legacy_stage_admission_observation AS member
    WHERE member.owner_user_id = actor
      AND member.admission_id = p_stage_admission_id
      AND member.observation_id = p_observation_id
      AND member.observation_sha256 = p_observation_sha256
  );
END
$function$;

ALTER FUNCTION memory.v5_local_stage_observation_allowed_v1(
  uuid, uuid, text
) OWNER TO memory_v5_local_entailment_maintainer;
REVOKE ALL ON FUNCTION
  memory.v5_local_stage_observation_allowed_v1(uuid, uuid, text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.v5_local_stage_observation_allowed_v1(uuid, uuid, text)
TO memory_v5_local_entailment_maintainer;

DO $extend_batch_resolver$
DECLARE
  function_definition text;
  old_tail constant text :=
'  ELSIF stage.decision IN (
    ''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''
  ) THEN
    RETURN QUERY
    SELECT value.batch_id
    FROM memory.v5_2_reviewed_observation_stage_admission AS value
    WHERE value.owner_user_id=actor
      AND value.admission_id=stage.admission_id;
  END IF;';
  new_tail constant text :=
'  ELSIF stage.decision IN (
    ''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''
  ) THEN
    RETURN QUERY
    SELECT value.batch_id
    FROM memory.v5_2_reviewed_observation_stage_admission AS value
    WHERE value.owner_user_id=actor
      AND value.admission_id=stage.admission_id;
  ELSIF stage.decision=''legacy_applied_stage_entailment'' THEN
    RETURN QUERY
    SELECT batch.batch_id
    FROM memory.relational_stage_batch AS batch
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=batch.owner_user_id
     AND request.request_id=stage.stage_request_id
     AND request.operation=''stage_packet''
     AND request.outcome=''applied''
     AND (request.result->>''batch_id'')::uuid=batch.batch_id
     AND request.manifest_sha256=batch.stage_manifest_sha256
    WHERE batch.owner_user_id=actor
      AND batch.batch_id=stage.source_legacy_stage_batch_id
      AND batch.evidence_id=stage.evidence_id
      AND batch.stage_manifest_sha256=stage.stage_bundle_sha256
      AND batch.extraction_packet_sha256=stage.packet_storage_sha256;
  END IF;';
BEGIN
  function_definition := pg_get_functiondef(
    'memory.v5_local_stage_admission_batch_v2(uuid)'::regprocedure
  );
  IF position(new_tail IN function_definition) = 0 THEN
    IF position(old_tail IN function_definition) = 0 THEN
      RAISE EXCEPTION 'stage admission batch resolver drifted';
    END IF;
    function_definition := replace(
      function_definition,
      old_tail,
      new_tail
    );
    EXECUTE function_definition;
  END IF;
END
$extend_batch_resolver$;

DO $extend_local_entailment$
DECLARE
  plan_definition text;
  register_definition text;
  old_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''';
  new_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'',''legacy_applied_stage_entailment''';
  old_plan_guard constant text :=
'    AND stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'')
    AND evidence.status=''active''';
  new_plan_guard constant text :=
'    AND stage.decision IN (''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage'',''legacy_applied_stage_entailment'')
    AND memory.v5_local_stage_observation_allowed_v1(
      stage.admission_id,o.observation_id,o.observation_sha256
    )
    AND evidence.status=''active''';
  old_register_guard constant text :=
'  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id;';
  new_register_guard constant text :=
'  WHERE stage.owner_user_id=actor
    AND stage.admission_id=p_stage_admission_id
    AND memory.v5_local_stage_observation_allowed_v1(
      stage.admission_id,o.observation_id,o.observation_sha256
    );';
BEGIN
  plan_definition := pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(new_plan_guard IN plan_definition) = 0 THEN
    IF position(old_plan_guard IN plan_definition) = 0 THEN
      RAISE EXCEPTION 'local entailment planner guard drifted';
    END IF;
    plan_definition := replace(
      plan_definition,
      old_plan_guard,
      new_plan_guard
    );
  END IF;
  IF position(new_decisions IN plan_definition) = 0 THEN
    IF position(old_decisions IN plan_definition) = 0 THEN
      RAISE EXCEPTION 'local entailment planner decisions drifted';
    END IF;
    plan_definition := replace(
      plan_definition,
      old_decisions,
      new_decisions
    );
  END IF;
  EXECUTE plan_definition;

  register_definition := pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(
      uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
      memory.observation_entailment_decision_v5,text,jsonb,text
    )'::regprocedure
  );
  IF position(new_register_guard IN register_definition) = 0 THEN
    IF position(old_register_guard IN register_definition) = 0 THEN
      RAISE EXCEPTION 'local entailment register guard drifted';
    END IF;
    register_definition := replace(
      register_definition,
      old_register_guard,
      new_register_guard
    );
  END IF;
  IF position(new_decisions IN register_definition) = 0 THEN
    IF position(old_decisions IN register_definition) = 0 THEN
      RAISE EXCEPTION 'local entailment register decisions drifted';
    END IF;
    register_definition := replace(
      register_definition,
      old_decisions,
      new_decisions
    );
  END IF;
  EXECUTE register_definition;
END
$extend_local_entailment$;

ALTER FUNCTION memory.v5_local_stage_admission_batch_v2(uuid)
  OWNER TO memory_v5_local_entailment_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_entailment_v1(integer)
  OWNER TO memory_v5_local_entailment_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_entailment_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
  memory.observation_entailment_decision_v5,text,jsonb,text
) OWNER TO memory_v5_local_entailment_maintainer;

REVOKE ALL ON FUNCTION
  memory.v5_local_stage_admission_batch_v2(uuid)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_entailment_v1(integer)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.register_owner_v5_local_entailment_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
    memory.observation_entailment_decision_v5,text,jsonb,text
  )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.v5_local_stage_admission_batch_v2(uuid),
  memory.plan_owner_v5_local_entailment_v1(integer),
  memory.register_owner_v5_local_entailment_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,
    memory.observation_entailment_decision_v5,text,jsonb,text
  )
TO brains_app;

COMMIT;
