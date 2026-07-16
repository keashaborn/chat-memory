BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 claim assessment migration requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL
     OR to_regclass('memory.projection_apply_event') IS NULL THEN
    RAISE EXCEPTION 'V5 claim assessment prerequisites are missing';
  END IF;
END
$prerequisite$;

DO $assessment_action$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type AS value
    JOIN pg_namespace AS namespace ON namespace.oid=value.typnamespace
    WHERE namespace.nspname='memory'
      AND value.typname='claim_assessment_action_v5'
  ) THEN
    CREATE TYPE memory.claim_assessment_action_v5 AS ENUM (
      'promote_supported',
      'promote_uncertain',
      'mark_disputed',
      'quarantine',
      'retract'
    );
  ELSIF (
    SELECT array_agg(enum.enumlabel::text ORDER BY enum.enumsortorder)
    FROM pg_enum AS enum
    WHERE enum.enumtypid='memory.claim_assessment_action_v5'::regtype
  ) <> ARRAY[
    'promote_supported',
    'promote_uncertain',
    'mark_disputed',
    'quarantine',
    'retract'
  ]::text[] THEN
    RAISE EXCEPTION 'existing V5 claim assessment action enum changed';
  END IF;
END
$assessment_action$;

DO $operation_contract$
DECLARE
  definition text;
  predecessor constant text :=
    $old$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text])))$old$;
  replacement constant text :=
    $new$CHECK ((operation = ANY (ARRAY['stage_packet'::text, 'review_resolution'::text, 'apply_resolution'::text, 'review_claim_assessment_v5'::text, 'apply_claim_assessment_v5'::text])))$new$;
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
        'apply_claim_assessment_v5'
      ));
  ELSE
    RAISE EXCEPTION 'relational operation contract changed';
  END IF;
END
$operation_contract$;

CREATE TABLE IF NOT EXISTS memory.claim_assessment_review_v5 (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  action memory.claim_assessment_action_v5 NOT NULL,
  expected_from_status memory.claim_status NOT NULL,
  target_status memory.claim_status NOT NULL,
  expected_revision_number integer NOT NULL CHECK (expected_revision_number > 0),
  expected_claim_state_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(expected_claim_state_sha256)),
  expected_evidence_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(expected_evidence_manifest_sha256)),
  support_score numeric(4,3) NOT NULL
    CHECK (support_score >= 0 AND support_score <= 1),
  opposition_score numeric(4,3) NOT NULL
    CHECK (opposition_score >= 0 AND opposition_score <= 1),
  claim_confidence numeric(4,3) NOT NULL
    CHECK (claim_confidence >= 0 AND claim_confidence <= 1),
  assessment_confidence numeric(4,3) NOT NULL
    CHECK (assessment_confidence >= 0 AND assessment_confidence <= 1),
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
  UNIQUE (owner_user_id,claim_id,authorization_manifest_sha256),
  FOREIGN KEY (owner_user_id,claim_id)
    REFERENCES memory.claim(owner_user_id,claim_id) ON DELETE RESTRICT,
  CHECK (
    (action='promote_supported' AND target_status='supported')
    OR (action='promote_uncertain' AND target_status='uncertain')
    OR (action='mark_disputed' AND target_status='disputed')
    OR (action='quarantine' AND target_status='quarantined')
    OR (action='retract' AND target_status='retracted')
  ),
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref)<=500)
);

CREATE INDEX IF NOT EXISTS claim_assessment_review_v5_owner_claim_idx
  ON memory.claim_assessment_review_v5(
    owner_user_id,claim_id,created_at DESC,review_id DESC
  );

CREATE TABLE IF NOT EXISTS memory.claim_assessment_apply_v5 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  review_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  from_status memory.claim_status NOT NULL,
  to_status memory.claim_status NOT NULL,
  prior_revision_number integer NOT NULL CHECK (prior_revision_number > 0),
  resulting_revision_number integer NOT NULL
    CHECK (resulting_revision_number=prior_revision_number+1),
  apply_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(apply_manifest_sha256)),
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,request_id),
  UNIQUE (owner_user_id,claim_id,review_id),
  UNIQUE (owner_user_id,claim_id,apply_manifest_sha256),
  FOREIGN KEY (owner_user_id,claim_id,review_id)
    REFERENCES memory.claim_assessment_review_v5(
      owner_user_id,claim_id,review_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,assessment_id)
    REFERENCES memory.claim_assessment(
      owner_user_id,assessment_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,claim_id,resulting_revision_number)
    REFERENCES memory.claim_revision(
      owner_user_id,claim_id,revision_number
    ) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS claim_assessment_apply_v5_owner_claim_idx
  ON memory.claim_assessment_apply_v5(owner_user_id,claim_id,created_at DESC);

DO $append_only$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'claim_assessment_review_v5',
    'claim_assessment_apply_v5'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I ON memory.%I',
      relation_name || '_append_only_guard', relation_name
    );
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only()',
      relation_name || '_append_only_guard', relation_name
    );
  END LOOP;
END
$append_only$;

ALTER TABLE memory.claim_assessment_review_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_assessment_review_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.claim_assessment_review_v5;
CREATE POLICY owner_isolation
  ON memory.claim_assessment_review_v5
  FOR ALL TO memory_v5_writer
  USING (
    owner_user_id=(SELECT memory.current_actor_user_id())
  )
  WITH CHECK (
    owner_user_id=(SELECT memory.current_actor_user_id())
  );

ALTER TABLE memory.claim_assessment_apply_v5 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_assessment_apply_v5 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.claim_assessment_apply_v5;
CREATE POLICY owner_isolation
  ON memory.claim_assessment_apply_v5
  FOR ALL TO memory_v5_writer
  USING (
    owner_user_id=(SELECT memory.current_actor_user_id())
  )
  WITH CHECK (
    owner_user_id=(SELECT memory.current_actor_user_id())
  );

CREATE OR REPLACE FUNCTION memory.claim_assessment_state_v5(
  p_claim_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  claim_row memory.claim%ROWTYPE;
  revision_number_value integer;
  claim_state_sha text;
  evidence_items jsonb;
  evidence_manifest text;
  active_support integer;
  active_opposition integer;
  active_qualifying integer;
  active_context integer;
  projection_row memory.projection_apply_event%ROWTYPE;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT stored.* INTO claim_row
  FROM memory.claim AS stored
  WHERE stored.owner_user_id=actor
    AND stored.claim_id=p_claim_id
    AND stored.canonical_key LIKE 'v5:%'
    AND stored.metadata->>'memory_contract'='memory_projection_v5';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped V5 claim not found'
      USING ERRCODE='P0002';
  END IF;

  SELECT COALESCE(max(revision.revision_number),0)
    INTO revision_number_value
  FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id=actor
    AND revision.claim_id=p_claim_id;
  IF revision_number_value < 1 THEN
    RAISE EXCEPTION 'V5 claim has no durable revision'
      USING ERRCODE='23514';
  END IF;

  claim_state_sha := memory.v5_digest_text(
    memory.v5_canonical_json_text(to_jsonb(claim_row))
  );

  SELECT COALESCE(jsonb_agg(item ORDER BY observation_id,stance),'[]'::jsonb),
         count(*) FILTER (
           WHERE stance='supports' AND evidence_status='active'
         )::integer,
         count(*) FILTER (
           WHERE stance='opposes' AND evidence_status='active'
         )::integer,
         count(*) FILTER (
           WHERE stance='qualifies' AND evidence_status='active'
         )::integer,
         count(*) FILTER (
           WHERE stance='context' AND evidence_status='active'
         )::integer
    INTO evidence_items,active_support,active_opposition,
         active_qualifying,active_context
  FROM (
    SELECT
      observation.observation_id,
      link.stance::text AS stance,
      evidence.status::text AS evidence_status,
      jsonb_build_object(
        'observation_id',observation.observation_id::text,
        'observation_sha256',observation.observation_sha256,
        'evidence_id',evidence.evidence_id::text,
        'evidence_content_sha256',evidence.content_sha256,
        'evidence_status',evidence.status::text,
        'stance',link.stance::text,
        'relevance',link.relevance::text,
        'temporal_sha256',temporal.normalized_sha256
      ) AS item
    FROM memory.claim_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id=link.owner_user_id
     AND observation.observation_id=link.observation_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
    LEFT JOIN memory.observation_temporal AS temporal
      ON temporal.owner_user_id=observation.owner_user_id
     AND temporal.observation_id=observation.observation_id
    WHERE link.owner_user_id=actor
      AND link.claim_id=p_claim_id
  ) AS source_rows;
  evidence_manifest := memory.v5_digest_text(
    memory.v5_canonical_json_text(evidence_items)
  );

  SELECT event.* INTO projection_row
  FROM memory.projection_apply_event AS event
  WHERE event.owner_user_id=actor
    AND event.resulting_claim_id=p_claim_id
    AND event.lane='claim'
    AND event.outcome='applied'
  ORDER BY event.resulting_claim_revision_number DESC,event.created_at DESC
  LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'V5 projection provenance not found'
      USING ERRCODE='23514';
  END IF;

  RETURN jsonb_build_object(
    'claim_id',claim_row.claim_id::text,
    'status',claim_row.status::text,
    'current_revision_number',revision_number_value,
    'claim_state_sha256',claim_state_sha,
    'evidence_manifest_sha256',evidence_manifest,
    'evidence_items',evidence_items,
    'active_support_count',active_support,
    'active_opposition_count',active_opposition,
    'active_qualifying_count',active_qualifying,
    'active_context_count',active_context,
    'projection_event_id',projection_row.event_id::text,
    'projection_apply_manifest_sha256',
      projection_row.apply_manifest_sha256
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_claim_assessment_review_v5(
  p_claim_id uuid,
  p_action memory.claim_assessment_action_v5,
  p_support_score numeric,
  p_opposition_score numeric,
  p_claim_confidence numeric,
  p_assessment_confidence numeric,
  p_reason_codes jsonb,
  p_rationale text,
  p_reviewer_type text,
  p_reviewer_ref text
)
RETURNS TABLE(
  claim_id uuid,
  from_status text,
  target_status text,
  current_revision_number integer,
  claim_state_sha256 text,
  evidence_manifest_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  state jsonb;
  source_status memory.claim_status;
  target memory.claim_status;
  support_count integer;
  active_count integer;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_claim_id IS NULL
     OR p_action IS NULL
     OR p_support_score IS NULL
     OR p_support_score<0 OR p_support_score>1
     OR p_opposition_score IS NULL
     OR p_opposition_score<0 OR p_opposition_score>1
     OR p_claim_confidence IS NULL
     OR p_claim_confidence<0 OR p_claim_confidence>1
     OR p_assessment_confidence IS NULL
     OR p_assessment_confidence<0 OR p_assessment_confidence>1
     OR NOT memory.v5_reason_codes_valid(p_reason_codes,20)
     OR jsonb_array_length(p_reason_codes)<1
     OR btrim(COALESCE(p_rationale,''))=''
     OR length(p_rationale)>2000
     OR p_reviewer_type NOT IN ('user','admin','system')
     OR length(COALESCE(p_reviewer_ref,''))>500 THEN
    RAISE EXCEPTION 'V5 claim assessment review inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  state := memory.claim_assessment_state_v5(p_claim_id);
  source_status := (state->>'status')::memory.claim_status;
  support_count := (state->>'active_support_count')::integer;
  active_count := support_count
    +(state->>'active_opposition_count')::integer
    +(state->>'active_qualifying_count')::integer
    +(state->>'active_context_count')::integer;
  target := CASE p_action
    WHEN 'promote_supported' THEN 'supported'::memory.claim_status
    WHEN 'promote_uncertain' THEN 'uncertain'::memory.claim_status
    WHEN 'mark_disputed' THEN 'disputed'::memory.claim_status
    WHEN 'quarantine' THEN 'quarantined'::memory.claim_status
    WHEN 'retract' THEN 'retracted'::memory.claim_status
  END;

  IF source_status NOT IN ('candidate','supported','uncertain','disputed')
     OR source_status=target THEN
    RAISE EXCEPTION 'V5 claim assessment transition is not allowed'
      USING ERRCODE='23514';
  END IF;
  IF target='supported'
     AND (
       support_count<1
       OR p_support_score<0.500
       OR p_claim_confidence<0.500
       OR p_opposition_score>p_support_score
     ) THEN
    RAISE EXCEPTION 'supported assessment lacks sufficient support'
      USING ERRCODE='23514';
  END IF;
  IF target='uncertain' AND active_count<1 THEN
    RAISE EXCEPTION 'uncertain assessment requires active evidence'
      USING ERRCODE='23514';
  END IF;
  IF target='disputed'
     AND (p_support_score<=0 OR p_opposition_score<=0) THEN
    RAISE EXCEPTION 'disputed assessment requires support and opposition'
      USING ERRCODE='23514';
  END IF;
  IF target='retracted'
     AND (p_opposition_score<0.500 OR p_claim_confidence>0.100) THEN
    RAISE EXCEPTION 'retracted assessment requires material opposition'
      USING ERRCODE='23514';
  END IF;

  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_claim_assessment_review_v5',
    actor::text,p_claim_id::text,p_action::text,source_status::text,target::text,
    state->>'current_revision_number',
    state->>'claim_state_sha256',
    state->>'evidence_manifest_sha256',
    state->>'projection_event_id',
    state->>'projection_apply_manifest_sha256',
    p_support_score::text,p_opposition_score::text,
    p_claim_confidence::text,p_assessment_confidence::text,
    memory.v5_canonical_json_text(p_reason_codes),
    btrim(p_rationale),p_reviewer_type,COALESCE(p_reviewer_ref,'')
  ));
  RETURN QUERY SELECT
    p_claim_id,source_status::text,target::text,
    (state->>'current_revision_number')::integer,
    state->>'claim_state_sha256',
    state->>'evidence_manifest_sha256',
    manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_claim_assessment_v5(
  p_request_id uuid,
  p_claim_id uuid,
  p_action memory.claim_assessment_action_v5,
  p_support_score numeric,
  p_opposition_score numeric,
  p_claim_confidence numeric,
  p_assessment_confidence numeric,
  p_reason_codes jsonb,
  p_rationale text,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  review_id uuid,
  outcome text,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_review memory.claim_assessment_review_v5%ROWTYPE;
  review_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'review request and manifest are required'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|claim_assessment_review|'||p_claim_id::text,0
  ));
  PERFORM 1 FROM memory.claim
  WHERE owner_user_id=actor AND claim_id=p_claim_id
  FOR UPDATE;
  SELECT * INTO preflight
  FROM memory.preflight_claim_assessment_review_v5(
    p_claim_id,p_action,p_support_score,p_opposition_score,
    p_claim_confidence,p_assessment_confidence,p_reason_codes,
    p_rationale,p_reviewer_type,p_reviewer_ref
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'claim assessment review manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_request_id;
  IF FOUND THEN
    IF existing_request.operation<>'review_claim_assessment_v5'
       OR existing_request.manifest_sha256
            <>p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'review_id')::uuid,
      'replayed',existing_request.result;
    RETURN;
  END IF;

  SELECT * INTO existing_review
  FROM memory.claim_assessment_review_v5 AS review
  WHERE review.owner_user_id=actor
    AND review.claim_id=p_claim_id
    AND review.authorization_manifest_sha256
          =p_authorization_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT existing_review.review_id,'replayed',
      jsonb_build_object(
        'review_id',existing_review.review_id,
        'claim_id',p_claim_id,
        'target_status',existing_review.target_status
      );
    RETURN;
  END IF;

  INSERT INTO memory.claim_assessment_review_v5(
    review_id,owner_user_id,claim_id,action,
    expected_from_status,target_status,expected_revision_number,
    expected_claim_state_sha256,expected_evidence_manifest_sha256,
    support_score,opposition_score,claim_confidence,
    assessment_confidence,reason_codes,rationale,
    reviewer_type,reviewer_ref,authorization_manifest_sha256
  ) VALUES (
    review_id_value,actor,p_claim_id,p_action,
    preflight.from_status::memory.claim_status,
    preflight.target_status::memory.claim_status,
    preflight.current_revision_number,
    preflight.claim_state_sha256,
    preflight.evidence_manifest_sha256,
    p_support_score,p_opposition_score,p_claim_confidence,
    p_assessment_confidence,p_reason_codes,btrim(p_rationale),
    p_reviewer_type,NULLIF(btrim(COALESCE(p_reviewer_ref,'')),''),
    p_authorization_manifest_sha256
  );
  result_value := jsonb_build_object(
    'review_id',review_id_value,
    'claim_id',p_claim_id,
    'target_status',preflight.target_status
  );
  INSERT INTO memory.relational_operation_request(
    request_id,owner_user_id,operation,target_key,
    manifest_sha256,outcome,result,invoked_by_session
  ) VALUES (
    p_request_id,actor,'review_claim_assessment_v5',p_claim_id::text,
    p_authorization_manifest_sha256,'applied',result_value,session_user
  );
  RETURN QUERY SELECT review_id_value,'applied',result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_claim_assessment_apply_v5(
  p_claim_id uuid,
  p_review_id uuid
)
RETURNS TABLE(
  claim_id uuid,
  review_id uuid,
  from_status text,
  target_status text,
  prior_revision_number integer,
  claim_state_sha256 text,
  evidence_manifest_sha256 text,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  state jsonb;
  review memory.claim_assessment_review_v5%ROWTYPE;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  state := memory.claim_assessment_state_v5(p_claim_id);
  SELECT stored.* INTO review
  FROM memory.claim_assessment_review_v5 AS stored
  WHERE stored.owner_user_id=actor
    AND stored.claim_id=p_claim_id
    AND stored.review_id=p_review_id;
  IF NOT FOUND
     OR review.review_id IS DISTINCT FROM (
       SELECT latest.review_id
       FROM memory.claim_assessment_review_v5 AS latest
       WHERE latest.owner_user_id=actor
         AND latest.claim_id=p_claim_id
       ORDER BY latest.created_at DESC,latest.review_id DESC
       LIMIT 1
     )
     OR review.expected_from_status::text<>state->>'status'
     OR review.expected_revision_number
          <>(state->>'current_revision_number')::integer
     OR review.expected_claim_state_sha256
          <>state->>'claim_state_sha256'
     OR review.expected_evidence_manifest_sha256
          <>state->>'evidence_manifest_sha256' THEN
    RAISE EXCEPTION 'latest claim assessment review is missing or stale'
      USING ERRCODE='23514';
  END IF;
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_claim_assessment_apply_v5',
    actor::text,p_claim_id::text,p_review_id::text,
    review.authorization_manifest_sha256,
    review.expected_from_status::text,review.target_status::text,
    state->>'current_revision_number',
    state->>'claim_state_sha256',
    state->>'evidence_manifest_sha256'
  ));
  RETURN QUERY SELECT
    p_claim_id,p_review_id,review.expected_from_status::text,
    review.target_status::text,review.expected_revision_number,
    state->>'claim_state_sha256',
    state->>'evidence_manifest_sha256',manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_claim_assessment_v5(
  p_request_id uuid,
  p_claim_id uuid,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  event_id uuid,
  outcome text,
  assessment_id uuid,
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
  existing_apply memory.claim_assessment_apply_v5%ROWTYPE;
  preflight record;
  review memory.claim_assessment_review_v5%ROWTYPE;
  claim_row memory.claim%ROWTYPE;
  prior_assessment uuid;
  assessment_id_value uuid := gen_random_uuid();
  event_id_value uuid := gen_random_uuid();
  revision_number_value integer;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL
     OR p_claim_id IS NULL
     OR p_review_id IS NULL
     OR NOT memory.v5_sha256_valid(p_apply_manifest_sha256) THEN
    RAISE EXCEPTION 'claim assessment apply identifiers are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|claim_assessment_apply|'||p_claim_id::text,0
  ));

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_request_id;
  IF FOUND THEN
    IF existing_request.operation<>'apply_claim_assessment_v5'
       OR existing_request.manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'event_id')::uuid,
      'replayed',
      (existing_request.result->>'assessment_id')::uuid,
      (existing_request.result->>'resulting_revision_number')::integer,
      0;
    RETURN;
  END IF;

  SELECT * INTO existing_apply
  FROM memory.claim_assessment_apply_v5 AS applied
  WHERE applied.owner_user_id=actor
    AND applied.claim_id=p_claim_id
    AND applied.review_id=p_review_id;
  IF FOUND THEN
    IF existing_apply.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'applied assessment manifest mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing_apply.event_id,'replayed',existing_apply.assessment_id,
      existing_apply.resulting_revision_number,0;
    RETURN;
  END IF;

  PERFORM 1 FROM memory.claim
  WHERE owner_user_id=actor AND claim_id=p_claim_id
  FOR UPDATE;
  PERFORM 1 FROM memory.claim_assessment_review_v5
  WHERE owner_user_id=actor
    AND claim_id=p_claim_id
    AND review_id=p_review_id
  FOR UPDATE;
  SELECT * INTO preflight
  FROM memory.preflight_claim_assessment_apply_v5(
    p_claim_id,p_review_id
  );
  IF preflight.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'claim assessment apply manifest mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT * INTO STRICT review
  FROM memory.claim_assessment_review_v5
  WHERE owner_user_id=actor
    AND claim_id=p_claim_id
    AND review_id=p_review_id;

  SELECT assessment.assessment_id INTO prior_assessment
  FROM memory.claim_assessment AS assessment
  WHERE assessment.owner_user_id=actor
    AND assessment.claim_id=p_claim_id
  ORDER BY assessment.assessed_at DESC,assessment.assessment_id DESC
  LIMIT 1;

  UPDATE memory.claim AS target SET
    status=review.target_status,
    confidence=review.claim_confidence,
    last_confirmed_at=CASE
      WHEN review.target_status='supported'
        THEN clock_timestamp()
      ELSE target.last_confirmed_at
    END
  WHERE target.owner_user_id=actor
    AND target.claim_id=p_claim_id
  RETURNING target.* INTO claim_row;

  INSERT INTO memory.claim_assessment(
    assessment_id,owner_user_id,claim_id,status,
    support_score,opposition_score,confidence,
    method,method_version,rationale,inputs,
    supersedes_assessment_id
  ) VALUES (
    assessment_id_value,actor,p_claim_id,review.target_status,
    review.support_score,review.opposition_score,
    review.assessment_confidence,
    'memory_v1_claim_assessment_v5','v5.1',review.rationale,
    jsonb_build_object(
      'review_id',review.review_id,
      'reason_codes',review.reason_codes,
      'claim_state_sha256',review.expected_claim_state_sha256,
      'evidence_manifest_sha256',
        review.expected_evidence_manifest_sha256
    ),
    prior_assessment
  );

  revision_number_value := preflight.prior_revision_number+1;
  INSERT INTO memory.claim_revision(
    owner_user_id,claim_id,revision_number,snapshot,
    reason,actor_type,actor_ref
  ) VALUES (
    actor,p_claim_id,revision_number_value,to_jsonb(claim_row),
    'claim_assessment_v5:'||review.action::text,
    review.reviewer_type,review.reviewer_ref
  );

  INSERT INTO memory.claim_assessment_apply_v5(
    event_id,owner_user_id,request_id,claim_id,review_id,
    assessment_id,from_status,to_status,prior_revision_number,
    resulting_revision_number,apply_manifest_sha256,
    invoked_by_session
  ) VALUES (
    event_id_value,actor,p_request_id,p_claim_id,p_review_id,
    assessment_id_value,
    preflight.from_status::memory.claim_status,
    preflight.target_status::memory.claim_status,
    preflight.prior_revision_number,revision_number_value,
    p_apply_manifest_sha256,session_user
  );

  result_value := jsonb_build_object(
    'event_id',event_id_value,
    'assessment_id',assessment_id_value,
    'claim_id',p_claim_id,
    'from_status',preflight.from_status,
    'target_status',preflight.target_status,
    'resulting_revision_number',revision_number_value
  );
  INSERT INTO memory.relational_operation_request(
    request_id,owner_user_id,operation,target_key,
    manifest_sha256,outcome,result,invoked_by_session
  ) VALUES (
    p_request_id,actor,'apply_claim_assessment_v5',p_claim_id::text,
    p_apply_manifest_sha256,'applied',result_value,session_user
  );
  RETURN QUERY SELECT
    event_id_value,'applied',assessment_id_value,
    revision_number_value,5;
END
$function$;

ALTER TABLE memory.claim_assessment_review_v5 OWNER TO memory_v5_writer;
ALTER TABLE memory.claim_assessment_apply_v5 OWNER TO memory_v5_writer;
ALTER FUNCTION memory.claim_assessment_state_v5(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.review_claim_assessment_v5(
  uuid,uuid,memory.claim_assessment_action_v5,
  numeric,numeric,numeric,numeric,jsonb,text,text,text,text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_assessment_apply_v5(uuid,uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)
  OWNER TO memory_v5_writer;

GRANT USAGE ON TYPE memory.claim_assessment_action_v5
  TO memory_v5_writer;
GRANT SELECT,INSERT ON
  memory.claim_assessment_review_v5,
  memory.claim_assessment_apply_v5
TO memory_v5_writer;
GRANT SELECT,INSERT ON memory.claim_assessment
  TO memory_v5_writer;
GRANT SELECT,UPDATE ON memory.claim
  TO memory_v5_writer;
GRANT SELECT,INSERT ON memory.claim_revision
  TO memory_v5_writer;

REVOKE ALL ON
  memory.claim_assessment_review_v5,
  memory.claim_assessment_apply_v5
FROM PUBLIC,brains_app;

REVOKE ALL ON FUNCTION memory.claim_assessment_state_v5(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.review_claim_assessment_v5(
  uuid,uuid,memory.claim_assessment_action_v5,
  numeric,numeric,numeric,numeric,jsonb,text,text,text,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_claim_assessment_apply_v5(uuid,uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.apply_claim_assessment_v5(
  uuid,uuid,uuid,text
) FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_claim_assessment_review_v5(
  uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,
  jsonb,text,text,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_claim_assessment_v5(
  uuid,uuid,memory.claim_assessment_action_v5,
  numeric,numeric,numeric,numeric,jsonb,text,text,text,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_claim_assessment_apply_v5(
  uuid,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_claim_assessment_v5(
  uuid,uuid,uuid,text
) TO brains_app;

COMMIT;
