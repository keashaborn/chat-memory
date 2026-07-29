BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.claim_assessment_state_v5(uuid)') IS NULL
     OR to_regprocedure(
       'memory.render_projection_claim_text_temporal_v5_2(uuid)'
     ) IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.claim_revision') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regclass('memory.relational_operation_request') IS NULL THEN
    RAISE EXCEPTION
      'V5.2 claim temporal reconciliation prerequisites are absent';
  END IF;
END
$preflight$;

DO $operation_contract$
DECLARE
  definition text;
  predecessor constant text :=
    $old$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text])))$old$;
  replacement constant text :=
    $new$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text, 'record_observation_entailment_v5'::text, 'review_claim_temporal_reconciliation_v5_2'::text, 'apply_claim_temporal_reconciliation_v5_2'::text])))$new$;
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
        'record_observation_entailment_v5',
        'review_claim_temporal_reconciliation_v5_2',
        'apply_claim_temporal_reconciliation_v5_2'
      ));
  ELSE
    RAISE EXCEPTION 'relational operation contract changed';
  END IF;
END
$operation_contract$;

CREATE TABLE IF NOT EXISTS memory.claim_temporal_reconciliation_review_v5_2 (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  corroborating_claim_id uuid NOT NULL,
  expected_revision_number integer NOT NULL
    CHECK (expected_revision_number > 0),
  expected_claim_state_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(expected_claim_state_sha256)),
  expected_observation_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(expected_observation_sha256)),
  expected_temporal_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(expected_temporal_sha256)),
  desired_canonical_text text NOT NULL
    CHECK (btrim(desired_canonical_text) <> ''),
  desired_canonical_text_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(desired_canonical_text_sha256)),
  desired_valid_to timestamptz NOT NULL,
  target_temporal_state text NOT NULL
    CHECK (target_temporal_state='historical'),
  reason_codes jsonb NOT NULL
    CHECK (memory.v5_reason_codes_valid(reason_codes,20)),
  rationale text NOT NULL
    CHECK (btrim(rationale)<>'' AND length(rationale)<=2000),
  reviewer_type text NOT NULL
    CHECK (reviewer_type IN ('user','admin','system')),
  reviewer_ref text,
  authorization_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(authorization_manifest_sha256)),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,review_id),
  UNIQUE (owner_user_id,claim_id,review_id),
  UNIQUE (owner_user_id,claim_id,observation_id),
  FOREIGN KEY (owner_user_id,claim_id)
    REFERENCES memory.claim(owner_user_id,claim_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,observation_id)
    REFERENCES memory.observation(owner_user_id,observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,corroborating_claim_id)
    REFERENCES memory.claim(owner_user_id,claim_id) ON DELETE RESTRICT,
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref)<=500)
);

CREATE TABLE IF NOT EXISTS memory.claim_temporal_reconciliation_apply_v5_2 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_stance memory.evidence_stance NOT NULL DEFAULT 'supports'
    CHECK (observation_stance='supports'),
  review_id uuid NOT NULL,
  prior_revision_number integer NOT NULL
    CHECK (prior_revision_number > 0),
  resulting_revision_number integer NOT NULL
    CHECK (resulting_revision_number=prior_revision_number+1),
  from_temporal_state text NOT NULL
    CHECK (from_temporal_state='current'),
  to_temporal_state text NOT NULL
    CHECK (to_temporal_state='historical'),
  apply_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(apply_manifest_sha256)),
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,request_id),
  UNIQUE (owner_user_id,claim_id,review_id),
  UNIQUE (owner_user_id,claim_id,observation_id),
  FOREIGN KEY (owner_user_id,claim_id,review_id)
    REFERENCES memory.claim_temporal_reconciliation_review_v5_2(
      owner_user_id,claim_id,review_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id,claim_id,observation_id,observation_stance
  )
    REFERENCES memory.claim_observation(
      owner_user_id,claim_id,observation_id,stance
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,claim_id,resulting_revision_number)
    REFERENCES memory.claim_revision(
      owner_user_id,claim_id,revision_number
    ) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS claim_temporal_review_owner_claim_idx
  ON memory.claim_temporal_reconciliation_review_v5_2(
    owner_user_id,claim_id,created_at DESC,review_id DESC
  );
CREATE INDEX IF NOT EXISTS claim_temporal_apply_owner_claim_idx
  ON memory.claim_temporal_reconciliation_apply_v5_2(
    owner_user_id,claim_id,created_at DESC,event_id DESC
  );

DO $append_only$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid=
      'memory.claim_temporal_reconciliation_review_v5_2'::regclass
      AND tgname='claim_temporal_reconciliation_review_v5_2_append_only'
  ) THEN
    CREATE TRIGGER claim_temporal_reconciliation_review_v5_2_append_only
      BEFORE UPDATE OR DELETE
      ON memory.claim_temporal_reconciliation_review_v5_2
      FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid=
      'memory.claim_temporal_reconciliation_apply_v5_2'::regclass
      AND tgname='claim_temporal_reconciliation_apply_v5_2_append_only'
  ) THEN
    CREATE TRIGGER claim_temporal_reconciliation_apply_v5_2_append_only
      BEFORE UPDATE OR DELETE
      ON memory.claim_temporal_reconciliation_apply_v5_2
      FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();
  END IF;
END
$append_only$;

ALTER TABLE memory.claim_temporal_reconciliation_review_v5_2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_temporal_reconciliation_review_v5_2
  FORCE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_temporal_reconciliation_apply_v5_2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_temporal_reconciliation_apply_v5_2
  FORCE ROW LEVEL SECURITY;

DO $policies$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname='memory'
      AND tablename='claim_temporal_reconciliation_review_v5_2'
      AND policyname='owner_isolation'
  ) THEN
    CREATE POLICY owner_isolation
      ON memory.claim_temporal_reconciliation_review_v5_2
      USING (owner_user_id=memory.current_actor_user_id())
      WITH CHECK (owner_user_id=memory.current_actor_user_id());
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname='memory'
      AND tablename='claim_temporal_reconciliation_apply_v5_2'
      AND policyname='owner_isolation'
  ) THEN
    CREATE POLICY owner_isolation
      ON memory.claim_temporal_reconciliation_apply_v5_2
      USING (owner_user_id=memory.current_actor_user_id())
      WITH CHECK (owner_user_id=memory.current_actor_user_id());
  END IF;
END
$policies$;

CREATE OR REPLACE FUNCTION
memory.preflight_claim_temporal_reconciliation_v5_2(
  p_claim_id uuid,
  p_observation_id uuid
)
RETURNS TABLE(
  claim_id uuid,
  observation_id uuid,
  corroborating_claim_id uuid,
  current_status text,
  from_temporal_state text,
  target_temporal_state text,
  current_revision_number integer,
  claim_state_sha256 text,
  observation_sha256 text,
  temporal_sha256 text,
  desired_canonical_text text,
  desired_canonical_text_sha256 text,
  desired_valid_to timestamptz,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  claim_row memory.claim%ROWTYPE;
  observation_row memory.observation%ROWTYPE;
  binding_row memory.observation_entity_binding%ROWTYPE;
  temporal_row memory.observation_temporal%ROWTYPE;
  death_claim_id uuid;
  current_revision integer;
  state jsonb;
  rendered text;
  rendered_sha text;
  valid_to_value timestamptz;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_claim_id IS NULL OR p_observation_id IS NULL THEN
    RAISE EXCEPTION 'temporal reconciliation identifiers are required'
      USING ERRCODE='22023';
  END IF;

  SELECT value.* INTO claim_row
  FROM memory.claim AS value
  WHERE value.owner_user_id=actor
    AND value.claim_id=p_claim_id
    AND value.status='supported'
    AND value.predicate='relationship.has_pet'
    AND value.object_entity_id IS NOT NULL
    AND value.valid_to IS NULL
    AND COALESCE(value.qualifiers->>'temporal_state','current')='current';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped current supported pet claim not found'
      USING ERRCODE='P0002';
  END IF;

  SELECT value.* INTO observation_row
  FROM memory.observation AS value
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=value.owner_user_id
   AND evidence.evidence_id=value.evidence_id
   AND evidence.status='active'
  WHERE value.owner_user_id=actor
    AND value.observation_id=p_observation_id
    AND value.predicate=claim_row.predicate
    AND value.polarity='affirmed'
    AND value.projection_class='direct_claim'
    AND value.object_mention_id IS NOT NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped active historical observation not found'
      USING ERRCODE='P0002';
  END IF;

  SELECT value.* INTO STRICT binding_row
  FROM memory.observation_entity_binding AS value
  WHERE value.owner_user_id=actor
    AND value.observation_id=p_observation_id
    AND value.subject_entity_id=claim_row.subject_entity_id
    AND value.object_entity_id=claim_row.object_entity_id;

  SELECT value.* INTO temporal_row
  FROM memory.observation_temporal AS value
  WHERE value.owner_user_id=actor
    AND value.observation_id=p_observation_id
    AND value.semantic='state_validity'
    AND value.shape='open_interval'
    AND value.basis='instant'
    AND value.certainty='bounded'
    AND value.anchored_to_source_time
    AND value.instant_range IS NOT NULL
    AND NOT upper_inf(value.instant_range)
    AND NOT upper_inc(value.instant_range)
    AND value.reason_codes ? 'historical_relationship_ended_before_source';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'bounded historical temporal state is absent'
      USING ERRCODE='23514';
  END IF;

  SELECT value.claim_id INTO death_claim_id
  FROM memory.claim AS value
  WHERE value.owner_user_id=actor
    AND value.subject_entity_id=claim_row.object_entity_id
    AND value.predicate='life_event.died'
    AND value.status='supported'
  ORDER BY value.updated_at DESC,value.claim_id
  LIMIT 1;
  IF death_claim_id IS NULL THEN
    RAISE EXCEPTION 'supported terminal life-event claim is absent'
      USING ERRCODE='23514';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.claim_observation AS link
    WHERE link.owner_user_id=actor
      AND link.claim_id=p_claim_id
      AND link.observation_id=p_observation_id
  ) THEN
    RAISE EXCEPTION 'historical observation is already linked'
      USING ERRCODE='23505';
  END IF;

  SELECT max(revision_number) INTO current_revision
  FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id=actor
    AND revision.claim_id=p_claim_id;
  IF current_revision IS NULL THEN
    RAISE EXCEPTION 'claim revision history is absent'
      USING ERRCODE='23514';
  END IF;

  state := memory.claim_assessment_state_v5(p_claim_id);
  rendered :=
    memory.render_projection_claim_text_temporal_v5_2(p_observation_id);
  IF rendered IS NULL
     OR rendered=claim_row.canonical_text
     OR rendered NOT LIKE '% formerly had a pet named %.'
     OR rendered LIKE '% may formerly %'
     OR rendered LIKE '% did not formerly %' THEN
    RAISE EXCEPTION 'historical canonical rendering is not authoritative'
      USING ERRCODE='23514';
  END IF;
  rendered_sha := memory.v5_digest_text(rendered);
  valid_to_value := upper(temporal_row.instant_range);
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_claim_temporal_reconciliation_v5_2',
    actor::text,p_claim_id::text,p_observation_id::text,
    death_claim_id::text,current_revision::text,
    state->>'claim_state_sha256',
    observation_row.observation_sha256,
    temporal_row.normalized_sha256,
    rendered_sha,valid_to_value::text
  ));

  RETURN QUERY SELECT
    p_claim_id,p_observation_id,death_claim_id,
    claim_row.status::text,'current','historical',
    current_revision,state->>'claim_state_sha256',
    observation_row.observation_sha256,
    temporal_row.normalized_sha256,
    rendered,rendered_sha,valid_to_value,manifest;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.review_claim_temporal_reconciliation_v5_2(
  p_request_id uuid,
  p_claim_id uuid,
  p_observation_id uuid,
  p_reason_codes jsonb,
  p_rationale text,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  review_id uuid,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  existing_request memory.relational_operation_request%ROWTYPE;
  preflight record;
  review_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR p_claim_id IS NULL
     OR p_observation_id IS NULL
     OR NOT memory.v5_reason_codes_valid(p_reason_codes,20)
     OR btrim(COALESCE(p_rationale,''))=''
     OR length(p_rationale)>2000
     OR p_reviewer_type NOT IN ('user','admin','system')
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'temporal reconciliation review input is invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|claim_temporal_review|'||p_claim_id::text,0
  ));
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_request_id;
  IF FOUND THEN
    IF existing_request.operation
         <>'review_claim_temporal_reconciliation_v5_2'
       OR existing_request.manifest_sha256
         <>p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'review_id')::uuid,'replayed',0;
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_claim_temporal_reconciliation_v5_2(
    p_claim_id,p_observation_id
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'temporal reconciliation review manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.claim_temporal_reconciliation_review_v5_2(
    review_id,owner_user_id,claim_id,observation_id,
    corroborating_claim_id,expected_revision_number,
    expected_claim_state_sha256,expected_observation_sha256,
    expected_temporal_sha256,desired_canonical_text,
    desired_canonical_text_sha256,desired_valid_to,
    target_temporal_state,reason_codes,rationale,
    reviewer_type,reviewer_ref,authorization_manifest_sha256
  ) VALUES (
    review_id_value,actor,p_claim_id,p_observation_id,
    preflight.corroborating_claim_id,
    preflight.current_revision_number,
    preflight.claim_state_sha256,preflight.observation_sha256,
    preflight.temporal_sha256,preflight.desired_canonical_text,
    preflight.desired_canonical_text_sha256,
    preflight.desired_valid_to,'historical',
    p_reason_codes,btrim(p_rationale),p_reviewer_type,
    NULLIF(btrim(COALESCE(p_reviewer_ref,'')),''),
    p_authorization_manifest_sha256
  );
  result_value := jsonb_build_object(
    'review_id',review_id_value,
    'claim_id',p_claim_id,
    'observation_id',p_observation_id,
    'target_temporal_state','historical'
  );
  INSERT INTO memory.relational_operation_request(
    request_id,owner_user_id,operation,target_key,
    manifest_sha256,outcome,result,invoked_by_session
  ) VALUES (
    p_request_id,actor,
    'review_claim_temporal_reconciliation_v5_2',
    p_claim_id::text,p_authorization_manifest_sha256,
    'applied',result_value,session_user
  );
  RETURN QUERY SELECT review_id_value,'applied',2;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.preflight_claim_temporal_reconciliation_apply_v5_2(
  p_claim_id uuid,
  p_review_id uuid
)
RETURNS TABLE(
  claim_id uuid,
  observation_id uuid,
  review_id uuid,
  prior_revision_number integer,
  resulting_revision_number integer,
  desired_canonical_text text,
  desired_valid_to timestamptz,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  review memory.claim_temporal_reconciliation_review_v5_2%ROWTYPE;
  preflight record;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT value.* INTO review
  FROM memory.claim_temporal_reconciliation_review_v5_2 AS value
  WHERE value.owner_user_id=actor
    AND value.claim_id=p_claim_id
    AND value.review_id=p_review_id;
  IF NOT FOUND
     OR review.review_id IS DISTINCT FROM (
       SELECT latest.review_id
       FROM memory.claim_temporal_reconciliation_review_v5_2 AS latest
       WHERE latest.owner_user_id=actor
         AND latest.claim_id=p_claim_id
       ORDER BY latest.created_at DESC,latest.review_id DESC
       LIMIT 1
     ) THEN
    RAISE EXCEPTION 'latest temporal reconciliation review is absent'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_claim_temporal_reconciliation_v5_2(
    p_claim_id,review.observation_id
  );
  IF review.expected_revision_number<>preflight.current_revision_number
     OR review.expected_claim_state_sha256<>preflight.claim_state_sha256
     OR review.expected_observation_sha256<>preflight.observation_sha256
     OR review.expected_temporal_sha256<>preflight.temporal_sha256
     OR review.desired_canonical_text
          <>preflight.desired_canonical_text
     OR review.desired_canonical_text_sha256
          <>preflight.desired_canonical_text_sha256
     OR review.desired_valid_to<>preflight.desired_valid_to
     OR review.authorization_manifest_sha256
          <>preflight.authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'temporal reconciliation review is stale'
      USING ERRCODE='23514';
  END IF;
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_claim_temporal_reconciliation_apply_v5_2',
    actor::text,p_claim_id::text,p_review_id::text,
    review.observation_id::text,
    review.authorization_manifest_sha256,
    review.expected_revision_number::text,
    review.expected_claim_state_sha256,
    review.expected_observation_sha256,
    review.expected_temporal_sha256,
    review.desired_canonical_text_sha256,
    review.desired_valid_to::text
  ));
  RETURN QUERY SELECT
    p_claim_id,review.observation_id,p_review_id,
    review.expected_revision_number,
    review.expected_revision_number+1,
    review.desired_canonical_text,review.desired_valid_to,manifest;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.apply_claim_temporal_reconciliation_v5_2(
  p_request_id uuid,
  p_claim_id uuid,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  event_id uuid,
  outcome text,
  resulting_revision_number integer,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_apply memory.claim_temporal_reconciliation_apply_v5_2%ROWTYPE;
  review memory.claim_temporal_reconciliation_review_v5_2%ROWTYPE;
  preflight record;
  claim_row memory.claim%ROWTYPE;
  event_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR p_claim_id IS NULL
     OR p_review_id IS NULL
     OR NOT memory.v5_sha256_valid(p_apply_manifest_sha256) THEN
    RAISE EXCEPTION 'temporal reconciliation apply input is invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|claim_temporal_apply|'||p_claim_id::text,0
  ));

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_request_id;
  IF FOUND THEN
    IF existing_request.operation
         <>'apply_claim_temporal_reconciliation_v5_2'
       OR existing_request.manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'event_id')::uuid,
      'replayed',
      (existing_request.result->>'resulting_revision_number')::integer,
      0;
    RETURN;
  END IF;

  SELECT * INTO existing_apply
  FROM memory.claim_temporal_reconciliation_apply_v5_2 AS value
  WHERE value.owner_user_id=actor
    AND value.claim_id=p_claim_id
    AND value.review_id=p_review_id;
  IF FOUND THEN
    IF existing_apply.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'applied temporal reconciliation manifest mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing_apply.event_id,'replayed',
      existing_apply.resulting_revision_number,0;
    RETURN;
  END IF;

  PERFORM 1 FROM memory.claim AS target
  WHERE target.owner_user_id=actor
    AND target.claim_id=p_claim_id
  FOR UPDATE;
  PERFORM 1
  FROM memory.claim_temporal_reconciliation_review_v5_2 AS selected_review
  WHERE selected_review.owner_user_id=actor
    AND selected_review.claim_id=p_claim_id
    AND selected_review.review_id=p_review_id
  FOR UPDATE;
  SELECT * INTO preflight
  FROM memory.preflight_claim_temporal_reconciliation_apply_v5_2(
    p_claim_id,p_review_id
  );
  IF preflight.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'temporal reconciliation apply manifest mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT * INTO STRICT review
  FROM memory.claim_temporal_reconciliation_review_v5_2 AS selected_review
  WHERE selected_review.owner_user_id=actor
    AND selected_review.claim_id=p_claim_id
    AND selected_review.review_id=p_review_id;

  INSERT INTO memory.claim_observation(
    owner_user_id,claim_id,observation_id,stance,relevance,rationale
  ) VALUES (
    actor,p_claim_id,review.observation_id,'supports',1,
    'memory_v1_claim_temporal_reconciliation_v5_2'
  );

  UPDATE memory.claim AS target SET
    canonical_text=review.desired_canonical_text,
    valid_to=review.desired_valid_to,
    last_confirmed_at=clock_timestamp(),
    qualifiers=target.qualifiers || jsonb_build_object(
      'temporal_state','historical',
      'valid_to_semantics','exclusive_upper_bound',
      'temporal_certainty','bounded',
      'temporal_shape','open_interval',
      'temporal_basis','instant',
      'temporal_source_form','implicit_source_time',
      'temporal_observation_id',review.observation_id::text
    ),
    metadata=target.metadata || jsonb_build_object(
      'temporal_reconciliation_contract',
        'memory_v1_claim_temporal_reconciliation_v5_2',
      'temporal_reconciliation_manifest_sha256',
        p_apply_manifest_sha256,
      'temporal_observation_sha256',
        review.expected_observation_sha256,
      'temporal_normalized_sha256',
        review.expected_temporal_sha256
    )
  WHERE target.owner_user_id=actor
    AND target.claim_id=p_claim_id
  RETURNING target.* INTO claim_row;

  INSERT INTO memory.claim_revision(
    owner_user_id,claim_id,revision_number,snapshot,
    reason,actor_type,actor_ref
  ) VALUES (
    actor,p_claim_id,preflight.resulting_revision_number,
    to_jsonb(claim_row),
    'claim_temporal_reconciliation_v5_2:historical',
    review.reviewer_type,review.reviewer_ref
  );

  INSERT INTO memory.claim_temporal_reconciliation_apply_v5_2(
    event_id,owner_user_id,request_id,claim_id,observation_id,
    observation_stance,review_id,
    prior_revision_number,resulting_revision_number,
    from_temporal_state,to_temporal_state,
    apply_manifest_sha256,invoked_by_session
  ) VALUES (
    event_id_value,actor,p_request_id,p_claim_id,
    review.observation_id,'supports',p_review_id,
    preflight.prior_revision_number,
    preflight.resulting_revision_number,
    'current','historical',p_apply_manifest_sha256,session_user
  );

  result_value := jsonb_build_object(
    'event_id',event_id_value,
    'claim_id',p_claim_id,
    'observation_id',review.observation_id,
    'resulting_revision_number',
      preflight.resulting_revision_number,
    'target_temporal_state','historical'
  );
  INSERT INTO memory.relational_operation_request(
    request_id,owner_user_id,operation,target_key,
    manifest_sha256,outcome,result,invoked_by_session
  ) VALUES (
    p_request_id,actor,
    'apply_claim_temporal_reconciliation_v5_2',
    p_claim_id::text,p_apply_manifest_sha256,
    'applied',result_value,session_user
  );
  RETURN QUERY SELECT
    event_id_value,'applied',
    preflight.resulting_revision_number,5;
END
$function$;

ALTER TABLE memory.claim_temporal_reconciliation_review_v5_2
  OWNER TO memory_v5_writer;
ALTER TABLE memory.claim_temporal_reconciliation_apply_v5_2
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_temporal_reconciliation_v5_2(
  uuid,uuid
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.review_claim_temporal_reconciliation_v5_2(
  uuid,uuid,uuid,jsonb,text,text,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_temporal_reconciliation_apply_v5_2(
  uuid,uuid
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_claim_temporal_reconciliation_v5_2(
  uuid,uuid,uuid,text
) OWNER TO memory_v5_writer;

GRANT SELECT,INSERT ON
  memory.claim_temporal_reconciliation_review_v5_2,
  memory.claim_temporal_reconciliation_apply_v5_2
TO memory_v5_writer;
GRANT SELECT,UPDATE ON memory.claim TO memory_v5_writer;
GRANT SELECT,INSERT ON memory.claim_revision TO memory_v5_writer;
GRANT SELECT,INSERT ON memory.claim_observation TO memory_v5_writer;

REVOKE ALL ON
  memory.claim_temporal_reconciliation_review_v5_2,
  memory.claim_temporal_reconciliation_apply_v5_2
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_claim_temporal_reconciliation_v5_2(uuid,uuid)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.review_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,jsonb,text,text,text,text
  )
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_claim_temporal_reconciliation_apply_v5_2(uuid,uuid)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.apply_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,text
  )
FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION
  memory.preflight_claim_temporal_reconciliation_v5_2(uuid,uuid)
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.review_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,jsonb,text,text,text,text
  )
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_claim_temporal_reconciliation_apply_v5_2(uuid,uuid)
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.apply_claim_temporal_reconciliation_v5_2(
    uuid,uuid,uuid,text
  )
TO brains_app;

COMMIT;
