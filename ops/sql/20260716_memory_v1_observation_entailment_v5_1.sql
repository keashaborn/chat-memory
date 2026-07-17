BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5.1 observation entailment migration requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.projection_plan_observation') IS NULL
     OR to_regclass('memory.relational_operation_request') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_sha256_valid(text)') IS NULL
     OR to_regprocedure('memory.v5_reason_codes_valid(jsonb,integer)') IS NULL
     OR to_regprocedure('memory.v5_source_spans_valid(jsonb)') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL THEN
    RAISE EXCEPTION 'V5.1 observation entailment prerequisites are absent';
  END IF;
  IF (SELECT rolbypassrls FROM pg_roles WHERE rolname='memory_v5_writer') THEN
    RAISE EXCEPTION 'memory_v5_writer must not bypass RLS';
  END IF;
END
$preflight$;

DO $decision_type$
DECLARE
  labels text[];
BEGIN
  IF to_regtype('memory.observation_entailment_decision_v5') IS NULL THEN
    CREATE TYPE memory.observation_entailment_decision_v5
      AS ENUM ('accepted','deferred');
  END IF;
  SELECT array_agg(enum.enumlabel::text ORDER BY enum.enumsortorder)
    INTO labels
  FROM pg_enum AS enum
  WHERE enum.enumtypid='memory.observation_entailment_decision_v5'::regtype;
  IF labels <> ARRAY['accepted','deferred']::text[] THEN
    RAISE EXCEPTION 'existing observation entailment decision enum changed';
  END IF;
END
$decision_type$;

DO $operation_contract$
DECLARE
  definition text;
  predecessor constant text :=
    $old$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text])))$old$;
  replacement constant text :=
    $new$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text])))$new$;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO definition
  FROM pg_constraint
  WHERE conrelid='memory.relational_operation_request'::regclass
    AND conname='relational_operation_request_operation_check';
  IF definition=replacement THEN
    NULL;
  ELSIF definition=predecessor THEN
    ALTER TABLE memory.relational_operation_request
      DROP CONSTRAINT relational_operation_request_operation_check;
    ALTER TABLE memory.relational_operation_request
      ADD CONSTRAINT relational_operation_request_operation_check
      CHECK (operation IN (
        'stage_packet',
        'review_resolution',
        'apply_resolution',
        'review_claim_assessment_v5',
        'apply_claim_assessment_v5',
        'record_observation_entailment_v5'
      ));
  ELSE
    RAISE EXCEPTION 'relational operation contract changed';
  END IF;
END
$operation_contract$;

CREATE OR REPLACE FUNCTION memory.v5_source_spans_match_text(
  value jsonb,
  source_text text
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path='pg_catalog'
AS $function$
DECLARE
  item jsonb;
  start_value integer;
  end_value integer;
  expected_sha text;
  actual_sha text;
BEGIN
  IF NOT memory.v5_source_spans_valid(value) THEN
    RETURN false;
  END IF;
  FOR item IN
    SELECT element FROM jsonb_array_elements(value) AS rows(element)
  LOOP
    start_value := (item->>'start')::integer;
    end_value := (item->>'end')::integer;
    expected_sha := item->>'span_sha256';
    IF end_value > char_length(source_text) THEN
      RETURN false;
    END IF;
    actual_sha := encode(
      public.digest(
        convert_to(
          substring(
            source_text FROM start_value+1 FOR end_value-start_value
          ),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    );
    IF actual_sha <> expected_sha THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
EXCEPTION
  WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_source_spans_cover(
  candidate_spans jsonb,
  required_spans jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path='pg_catalog'
AS $function$
DECLARE
  required_item jsonb;
BEGIN
  IF NOT memory.v5_source_spans_valid(candidate_spans)
     OR NOT memory.v5_source_spans_valid(required_spans) THEN
    RETURN false;
  END IF;
  FOR required_item IN
    SELECT element
    FROM jsonb_array_elements(required_spans) AS rows(element)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(candidate_spans) AS rows(element)
      WHERE (rows.element->>'start')::integer
              <=(required_item->>'start')::integer
        AND (rows.element->>'end')::integer
              >=(required_item->>'end')::integer
    ) THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
EXCEPTION
  WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END
$function$;

CREATE TABLE IF NOT EXISTS memory.observation_entailment_v5 (
  decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_sha256 text NOT NULL,
  evidence_id uuid NOT NULL,
  evidence_content_sha256 text NOT NULL,
  policy_version text NOT NULL,
  decision memory.observation_entailment_decision_v5 NOT NULL,
  reason_code text NOT NULL,
  source_spans jsonb NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  assessor_type text NOT NULL,
  assessor_ref text NOT NULL,
  invoked_by_session text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, observation_id, policy_version),
  FOREIGN KEY (owner_user_id, observation_id, observation_sha256)
    REFERENCES memory.observation(
      owner_user_id,observation_id,observation_sha256
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (policy_version='memory_v1_predicate_entailment_v5_1'),
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (memory.v5_sha256_valid(evidence_content_sha256)),
  CHECK (memory.v5_sha256_valid(authorization_manifest_sha256)),
  CHECK (memory.v5_source_spans_valid(source_spans)),
  CHECK (
    (decision='accepted'
      AND reason_code='predicate_entailment_v5_1_accepted')
    OR
    (decision='deferred'
      AND reason_code IN (
        'source_contradicts_predicate',
        'predicate_semantics_unresolved'
      ))
  ),
  CHECK (assessor_type IN ('system','admin')),
  CHECK (btrim(assessor_ref)<>'' AND length(assessor_ref)<=500),
  CHECK (btrim(invoked_by_session)<>'' AND length(invoked_by_session)<=120)
);

CREATE INDEX IF NOT EXISTS observation_entailment_owner_evidence_idx
  ON memory.observation_entailment_v5(owner_user_id,evidence_id);

CREATE OR REPLACE FUNCTION memory.preflight_observation_entailment_v5(
  p_observation_id uuid,
  p_decision memory.observation_entailment_decision_v5,
  p_reason_code text,
  p_source_spans jsonb,
  p_assessor_type text,
  p_assessor_ref text
)
RETURNS TABLE(
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  policy_version text,
  decision text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  observation_row memory.observation%ROWTYPE;
  evidence_row memory.evidence%ROWTYPE;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_observation_id IS NULL
     OR p_decision IS NULL
     OR btrim(COALESCE(p_reason_code,''))=''
     OR p_source_spans IS NULL
     OR p_assessor_type NOT IN ('system','admin')
     OR btrim(COALESCE(p_assessor_ref,''))=''
     OR length(p_assessor_ref)>500 THEN
    RAISE EXCEPTION 'observation entailment inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  IF (p_decision='accepted'
      AND p_reason_code<>'predicate_entailment_v5_1_accepted')
     OR
     (p_decision='deferred'
      AND p_reason_code NOT IN (
        'source_contradicts_predicate',
        'predicate_semantics_unresolved'
      )) THEN
    RAISE EXCEPTION 'observation entailment decision/reason mismatch'
      USING ERRCODE='23514';
  END IF;
  IF NOT memory.v5_source_spans_valid(p_source_spans) THEN
    RAISE EXCEPTION 'observation entailment source spans are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT stored.* INTO observation_row
  FROM memory.observation AS stored
  WHERE stored.owner_user_id=actor
    AND stored.observation_id=p_observation_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped observation not found'
      USING ERRCODE='P0002';
  END IF;
  SELECT stored.* INTO evidence_row
  FROM memory.evidence AS stored
  WHERE stored.owner_user_id=actor
    AND stored.evidence_id=observation_row.evidence_id
    AND stored.status='active';
  IF NOT FOUND
     OR evidence_row.content IS NULL
     OR evidence_row.content_sha256 IS NULL
     OR memory.v5_digest_text(evidence_row.content)
          <>evidence_row.content_sha256 THEN
    RAISE EXCEPTION 'active source evidence is absent or hash-invalid'
      USING ERRCODE='23514';
  END IF;
  IF NOT memory.v5_source_spans_match_text(
    p_source_spans,evidence_row.content
  ) THEN
    RAISE EXCEPTION 'observation entailment spans do not match source evidence'
      USING ERRCODE='23514';
  END IF;
  IF NOT memory.v5_source_spans_cover(
    p_source_spans,observation_row.source_spans
  ) THEN
    RAISE EXCEPTION
      'observation entailment spans do not cover observation source spans'
      USING ERRCODE='23514';
  END IF;

  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_observation_entailment_v5_1',
    actor::text,
    observation_row.observation_id::text,
    observation_row.observation_sha256,
    evidence_row.evidence_id::text,
    evidence_row.content_sha256,
    'memory_v1_predicate_entailment_v5_1',
    p_decision::text,
    p_reason_code,
    memory.v5_canonical_json_text(p_source_spans),
    p_assessor_type,
    btrim(p_assessor_ref)
  ));
  RETURN QUERY SELECT
    observation_row.observation_id,
    observation_row.observation_sha256,
    evidence_row.evidence_id,
    evidence_row.content_sha256,
    'memory_v1_predicate_entailment_v5_1'::text,
    p_decision::text,
    manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_observation_entailment_v5(
  p_request_id uuid,
  p_observation_id uuid,
  p_decision memory.observation_entailment_decision_v5,
  p_reason_code text,
  p_source_spans jsonb,
  p_assessor_type text,
  p_assessor_ref text,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  decision_id uuid,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_decision memory.observation_entailment_v5%ROWTYPE;
  decision_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'observation entailment request and manifest are required'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|observation_entailment|'
      ||COALESCE(p_observation_id::text,''),
    0
  ));

  SELECT * INTO preflight
  FROM memory.preflight_observation_entailment_v5(
    p_observation_id,p_decision,p_reason_code,p_source_spans,
    p_assessor_type,p_assessor_ref
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'observation entailment manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_request_id;
  IF FOUND THEN
    IF existing_request.operation<>'record_observation_entailment_v5'
       OR existing_request.manifest_sha256
            <>p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'decision_id')::uuid,
      'replayed',0;
    RETURN;
  END IF;

  SELECT stored.* INTO existing_decision
  FROM memory.observation_entailment_v5 AS stored
  WHERE stored.owner_user_id=actor
    AND stored.observation_id=p_observation_id
    AND stored.policy_version='memory_v1_predicate_entailment_v5_1';
  IF FOUND THEN
    RAISE EXCEPTION
      'observation decision already belongs to a different request_id'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.observation_entailment_v5(
    decision_id,owner_user_id,observation_id,observation_sha256,
    evidence_id,evidence_content_sha256,policy_version,decision,
    reason_code,source_spans,authorization_manifest_sha256,
    assessor_type,assessor_ref,invoked_by_session
  ) VALUES (
    decision_id_value,actor,preflight.observation_id,
    preflight.observation_sha256,preflight.evidence_id,
    preflight.evidence_content_sha256,preflight.policy_version,
    p_decision,p_reason_code,p_source_spans,
    p_authorization_manifest_sha256,p_assessor_type,
    btrim(p_assessor_ref),session_user
  );
  result_value := jsonb_build_object(
    'decision_id',decision_id_value,
    'observation_id',p_observation_id,
    'decision',p_decision,
    'policy_version','memory_v1_predicate_entailment_v5_1'
  );
  INSERT INTO memory.relational_operation_request(
    request_id,owner_user_id,operation,target_key,
    manifest_sha256,outcome,result,invoked_by_session
  ) VALUES (
    p_request_id,actor,'record_observation_entailment_v5',
    p_observation_id::text,p_authorization_manifest_sha256,
    'applied',result_value,session_user
  );
  RETURN QUERY SELECT decision_id_value,'applied',2;
END
$function$;

CREATE OR REPLACE FUNCTION memory.observation_entailment_allows_projection_v5(
  p_observation_id uuid,
  p_observation_sha256 text
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_observation_id IS NULL
     OR NOT memory.v5_sha256_valid(p_observation_sha256) THEN
    RAISE EXCEPTION 'projection entailment identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM memory.observation_entailment_v5 AS entailment
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=entailment.owner_user_id
     AND evidence.evidence_id=entailment.evidence_id
     AND evidence.status='active'
     AND evidence.content_sha256=entailment.evidence_content_sha256
    WHERE entailment.owner_user_id=actor
      AND entailment.observation_id=p_observation_id
      AND entailment.observation_sha256=p_observation_sha256
      AND entailment.policy_version='memory_v1_predicate_entailment_v5_1'
      AND entailment.decision='accepted'
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_projection_observation_entailment_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF NEW.owner_user_id<>actor
     OR NOT memory.observation_entailment_allows_projection_v5(
       NEW.observation_id,NEW.observation_sha256
     ) THEN
    RAISE EXCEPTION 'projection observation lacks accepted V5.1 entailment'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER TABLE memory.observation_entailment_v5 OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_source_spans_match_text(jsonb,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_source_spans_cover(jsonb,jsonb)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_observation_entailment_v5(
  uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.record_observation_entailment_v5(
  uuid,uuid,memory.observation_entailment_decision_v5,
  text,jsonb,text,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.observation_entailment_allows_projection_v5(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_observation_entailment_v5()
  OWNER TO memory_v5_writer;

ALTER TABLE memory.observation_entailment_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.observation_entailment_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.observation_entailment_v5;
CREATE POLICY owner_isolation
  ON memory.observation_entailment_v5
  TO memory_v5_writer
  USING (
    owner_user_id=(SELECT memory.current_actor_user_id())
  )
  WITH CHECK (
    owner_user_id=(SELECT memory.current_actor_user_id())
  );

DROP TRIGGER IF EXISTS observation_entailment_v5_append_only_guard
  ON memory.observation_entailment_v5;
CREATE TRIGGER observation_entailment_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.observation_entailment_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

DROP TRIGGER IF EXISTS projection_plan_observation_entailment_guard
  ON memory.projection_plan_observation;
CREATE TRIGGER projection_plan_observation_entailment_guard
BEFORE INSERT ON memory.projection_plan_observation
FOR EACH ROW
EXECUTE FUNCTION memory.guard_projection_observation_entailment_v5();

GRANT USAGE ON TYPE memory.observation_entailment_decision_v5
  TO memory_v5_writer,brains_app;
GRANT SELECT,INSERT ON memory.observation_entailment_v5
  TO memory_v5_writer;
GRANT SELECT ON memory.observation,memory.evidence
  TO memory_v5_writer;
GRANT SELECT,INSERT ON memory.relational_operation_request
  TO memory_v5_writer;

REVOKE ALL ON memory.observation_entailment_v5
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.v5_source_spans_match_text(jsonb,text)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.v5_source_spans_cover(jsonb,jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_observation_entailment_v5(
  uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.record_observation_entailment_v5(
  uuid,uuid,memory.observation_entailment_decision_v5,
  text,jsonb,text,text,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.observation_entailment_allows_projection_v5(
  uuid,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.guard_projection_observation_entailment_v5()
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_observation_entailment_v5(
  uuid,memory.observation_entailment_decision_v5,text,jsonb,text,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_observation_entailment_v5(
  uuid,uuid,memory.observation_entailment_decision_v5,
  text,jsonb,text,text,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.observation_entailment_allows_projection_v5(
  uuid,text
) TO brains_app;

COMMIT;
