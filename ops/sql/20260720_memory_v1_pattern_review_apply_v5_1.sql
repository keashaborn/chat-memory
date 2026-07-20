BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'pattern review/apply V5.1 migration requires sage';
  END IF;
  IF to_regrole('memory_v5_epistemic_writer') IS NULL
     OR to_regprocedure('memory.require_v5_epistemic_writer_context()') IS NULL
     OR to_regprocedure('memory.epistemic_packet_sha256_v5_1(jsonb)') IS NULL
     OR to_regclass('memory.pattern_hypothesis_v5_1') IS NULL
     OR to_regclass('memory.pattern_hypothesis_revision_v5_1') IS NULL
     OR to_regclass('memory.pattern_observation_link_v5_1') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL THEN
    RAISE EXCEPTION 'pattern review/apply V5.1 prerequisites are absent';
  END IF;
END
$preflight$;

DO $types$
BEGIN
  BEGIN
    CREATE TYPE memory.pattern_review_decision_v5_1 AS ENUM (
      'authorized','rejected','deferred'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
END
$types$;

CREATE TABLE IF NOT EXISTS memory.pattern_review_v5_1 (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  pattern_key_sha256 text NOT NULL,
  proposal jsonb NOT NULL,
  proposal_sha256 text NOT NULL,
  decision memory.pattern_review_decision_v5_1 NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text NOT NULL,
  review_reason_codes jsonb NOT NULL,
  expected_pattern_id uuid,
  expected_revision_number integer NOT NULL,
  expected_head_state_sha256 text NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,review_id),
  UNIQUE(owner_user_id,pattern_key_sha256,authorization_manifest_sha256),
  FOREIGN KEY(owner_user_id,expected_pattern_id)
    REFERENCES memory.pattern_hypothesis_v5_1(owner_user_id,pattern_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(pattern_key_sha256)),
  CHECK (jsonb_typeof(proposal)='object' AND pg_column_size(proposal)<=262144),
  CHECK (memory.v5_sha256_valid(proposal_sha256)),
  CHECK (reviewer_type IN ('user','admin','system')),
  CHECK (btrim(reviewer_ref)<>'' AND length(reviewer_ref)<=500),
  CHECK (memory.v5_reason_codes_valid(review_reason_codes,32)
         AND jsonb_array_length(review_reason_codes)>0),
  CHECK (expected_revision_number>=0),
  CHECK ((expected_pattern_id IS NULL)=(expected_revision_number=0)),
  CHECK (memory.v5_sha256_valid(expected_head_state_sha256)),
  CHECK (memory.v5_sha256_valid(authorization_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.pattern_review_observation_v5_1 (
  owner_user_id uuid NOT NULL,
  review_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  observation_role memory.pattern_observation_role_v5_1 NOT NULL,
  episode_key_sha256 text NOT NULL,
  independence_key_sha256 text NOT NULL,
  temporal_bucket_sha256 text NOT NULL,
  observation_sha256 text NOT NULL,
  evidence_content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,review_id,observation_id),
  FOREIGN KEY(owner_user_id,review_id)
    REFERENCES memory.pattern_review_v5_1(owner_user_id,review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id,observation_id)
    REFERENCES memory.observation(owner_user_id,evidence_id,observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(episode_key_sha256)),
  CHECK (memory.v5_sha256_valid(independence_key_sha256)),
  CHECK (memory.v5_sha256_valid(temporal_bucket_sha256)),
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (memory.v5_sha256_valid(evidence_content_sha256))
);

CREATE UNIQUE INDEX IF NOT EXISTS pattern_review_observation_v5_1_episode_uq
  ON memory.pattern_review_observation_v5_1(
    owner_user_id,review_id,observation_role,episode_key_sha256
  ) WHERE observation_role IN ('occurrence','counterexample');
CREATE INDEX IF NOT EXISTS pattern_review_observation_v5_1_observation_idx
  ON memory.pattern_review_observation_v5_1(owner_user_id,observation_id);
CREATE INDEX IF NOT EXISTS pattern_review_observation_v5_1_evidence_idx
  ON memory.pattern_review_observation_v5_1(owner_user_id,evidence_id);

CREATE TABLE IF NOT EXISTS memory.pattern_apply_event_v5_1 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  review_id uuid NOT NULL,
  pattern_id uuid NOT NULL,
  prior_revision_number integer NOT NULL,
  resulting_revision_number integer NOT NULL,
  pattern_revision_id uuid NOT NULL,
  apply_manifest_sha256 text NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,event_id),
  UNIQUE(owner_user_id,request_id),
  UNIQUE(owner_user_id,review_id),
  UNIQUE(owner_user_id,apply_manifest_sha256),
  FOREIGN KEY(owner_user_id,review_id)
    REFERENCES memory.pattern_review_v5_1(owner_user_id,review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,pattern_id,pattern_revision_id)
    REFERENCES memory.pattern_hypothesis_revision_v5_1(
      owner_user_id,pattern_id,revision_id
    ) ON DELETE RESTRICT,
  CHECK (prior_revision_number>=0),
  CHECK (resulting_revision_number=prior_revision_number+1),
  CHECK (memory.v5_sha256_valid(apply_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.pattern_operation_request_v5_1 (
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  operation text NOT NULL,
  manifest_sha256 text NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,request_id),
  UNIQUE(owner_user_id,operation,manifest_sha256),
  CHECK (operation IN ('review_pattern','apply_pattern')),
  CHECK (memory.v5_sha256_valid(manifest_sha256)),
  CHECK (jsonb_typeof(result)='object' AND pg_column_size(result)<=16384)
);

CREATE INDEX IF NOT EXISTS pattern_review_v5_1_owner_key_idx
  ON memory.pattern_review_v5_1(
    owner_user_id,pattern_key_sha256,created_at DESC,review_id DESC
  );
CREATE INDEX IF NOT EXISTS pattern_review_v5_1_expected_pattern_idx
  ON memory.pattern_review_v5_1(owner_user_id,expected_pattern_id)
  WHERE expected_pattern_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pattern_apply_event_v5_1_owner_pattern_idx
  ON memory.pattern_apply_event_v5_1(
    owner_user_id,pattern_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS pattern_apply_event_v5_1_revision_idx
  ON memory.pattern_apply_event_v5_1(
    owner_user_id,pattern_id,pattern_revision_id
  );
CREATE INDEX IF NOT EXISTS pattern_operation_request_v5_1_owner_time_idx
  ON memory.pattern_operation_request_v5_1(owner_user_id,created_at DESC);

DO $append_only$
DECLARE relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_review_v5_1','pattern_review_observation_v5_1',
    'pattern_apply_event_v5_1','pattern_operation_request_v5_1'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON memory.%I',
      relation_name||'_append_only_guard',relation_name);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only()',
      relation_name||'_append_only_guard',relation_name
    );
  END LOOP;
END
$append_only$;

DO $rls$
DECLARE relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_review_v5_1','pattern_review_observation_v5_1',
    'pattern_apply_event_v5_1','pattern_operation_request_v5_1'
  ] LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I',relation_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'FOR ALL TO memory_v5_epistemic_writer '
      'USING (owner_user_id=(SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()))',
      relation_name
    );
    EXECUTE format('REVOKE ALL ON memory.%I FROM PUBLIC,brains_app',relation_name);
  END LOOP;
END
$rls$;

DROP POLICY IF EXISTS pattern_v5_1_reference_read
  ON memory.observation_entity_binding;
CREATE POLICY pattern_v5_1_reference_read
  ON memory.observation_entity_binding
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.pattern_proposal_sha256_v5_1(proposal jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
  SELECT memory.v5_digest_text(
    memory.v5_canonical_json_text(proposal-'proposal_sha256')
  )
$function$;

CREATE OR REPLACE FUNCTION memory.pattern_proposal_shape_valid_v5_1(
  proposal jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  item jsonb;
  key_count integer;
BEGIN
  IF jsonb_typeof(proposal)<>'object'
     OR NOT memory.v5_jsonb_exact_keys(proposal,ARRAY[
       'contract_version','policy_version','source_registry_version',
       'pattern_key_sha256','pattern_kind','subject_entity_id',
       'secondary_entity_id','predicate_family','sensitivity','pattern_state',
       'occurrence_count','counterexample_count','independent_episode_count',
       'distinct_temporal_bucket_count','valid_from','valid_to',
       'automatic_review_eligible','identity_inference_forbidden',
       'causal_inference_forbidden','reason_codes','method_version',
       'input_manifest_sha256','observation_items','proposal_sha256'
     ])
     OR proposal->>'contract_version'<>'memory_v1_pattern_review_v5_1'
     OR proposal->>'policy_version'
          <>'memory_v1_epistemic_pattern_salience_policy_v5_1'
     OR proposal->>'source_registry_version'<>'memory_predicate_registry_v5_1'
     OR NOT memory.v5_sha256_valid(proposal->>'pattern_key_sha256')
     OR proposal->>'pattern_kind' NOT IN (
       'recurrence','persistence','transition','trend','co_occurrence','sequence'
     )
     OR proposal->>'subject_entity_id' IS NULL
     OR proposal->>'predicate_family' !~ '^[a-z][a-z0-9_.:-]{1,239}$'
     OR proposal->>'sensitivity' NOT IN ('low','medium','high','restricted')
     OR proposal->>'pattern_state' NOT IN (
       'insufficient','emerging','supported','contested','ended',
       'superseded','retracted'
     )
     OR jsonb_typeof(proposal->'occurrence_count')<>'number'
     OR jsonb_typeof(proposal->'counterexample_count')<>'number'
     OR jsonb_typeof(proposal->'independent_episode_count')<>'number'
     OR jsonb_typeof(proposal->'distinct_temporal_bucket_count')<>'number'
     OR jsonb_typeof(proposal->'automatic_review_eligible')<>'boolean'
     OR proposal->'identity_inference_forbidden'<>'true'::jsonb
     OR proposal->'causal_inference_forbidden'<>'true'::jsonb
     OR NOT memory.v5_reason_codes_valid(proposal->'reason_codes',32)
     OR jsonb_array_length(proposal->'reason_codes')<1
     OR proposal->>'method_version' !~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$'
     OR NOT memory.v5_sha256_valid(proposal->>'input_manifest_sha256')
     OR jsonb_typeof(proposal->'observation_items')<>'array'
     OR jsonb_array_length(proposal->'observation_items') NOT BETWEEN 1 AND 256
     OR NOT memory.v5_sha256_valid(proposal->>'proposal_sha256')
     OR proposal->>'proposal_sha256'<>memory.pattern_proposal_sha256_v5_1(proposal)
     OR (proposal->'secondary_entity_id'<>'null'::jsonb
         AND proposal->>'secondary_entity_id'=proposal->>'subject_entity_id')
     OR (proposal->'valid_from'<>'null'::jsonb
         AND proposal->>'valid_from' !~ '^\d{4}-\d{2}-\d{2}$')
     OR (proposal->'valid_to'<>'null'::jsonb
         AND proposal->>'valid_to' !~ '^\d{4}-\d{2}-\d{2}$') THEN
    RETURN false;
  END IF;

  IF (proposal->>'occurrence_count')::numeric
       <>trunc((proposal->>'occurrence_count')::numeric)
     OR (proposal->>'counterexample_count')::numeric
       <>trunc((proposal->>'counterexample_count')::numeric)
     OR (proposal->>'independent_episode_count')::numeric
       <>trunc((proposal->>'independent_episode_count')::numeric)
     OR (proposal->>'distinct_temporal_bucket_count')::numeric
       <>trunc((proposal->>'distinct_temporal_bucket_count')::numeric)
     OR (proposal->>'occurrence_count')::integer<0
     OR (proposal->>'counterexample_count')::integer<0
     OR (proposal->>'independent_episode_count')::integer<0
     OR (proposal->>'distinct_temporal_bucket_count')::integer<0
     OR (proposal->>'independent_episode_count')::integer
          >(proposal->>'occurrence_count')::integer
     OR (proposal->'valid_from'<>'null'::jsonb
         AND proposal->'valid_to'<>'null'::jsonb
         AND (proposal->>'valid_to')::date<(proposal->>'valid_from')::date) THEN
    RETURN false;
  END IF;

  FOR item IN SELECT value FROM jsonb_array_elements(proposal->'observation_items')
  LOOP
    IF jsonb_typeof(item)<>'object'
       OR NOT memory.v5_jsonb_exact_keys(item,ARRAY[
         'observation_id','evidence_id','observation_role',
         'episode_key_sha256','independence_key_sha256',
         'temporal_bucket_sha256','observation_sha256',
         'evidence_content_sha256'
       ])
       OR item->>'observation_role' NOT IN (
         'occurrence','counterexample','boundary','context'
       )
       OR NOT memory.v5_sha256_valid(item->>'episode_key_sha256')
       OR NOT memory.v5_sha256_valid(item->>'independence_key_sha256')
       OR NOT memory.v5_sha256_valid(item->>'temporal_bucket_sha256')
       OR NOT memory.v5_sha256_valid(item->>'observation_sha256')
       OR NOT memory.v5_sha256_valid(item->>'evidence_content_sha256') THEN
      RETURN false;
    END IF;
    PERFORM (item->>'observation_id')::uuid,(item->>'evidence_id')::uuid;
  END LOOP;
  RETURN true;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
  OR datetime_field_overflow THEN
  RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.pattern_head_state_v5_1(
  p_pattern_key_sha256 text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  head memory.pattern_hypothesis_v5_1%ROWTYPE;
  revision memory.pattern_hypothesis_revision_v5_1%ROWTYPE;
  state jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF NOT memory.v5_sha256_valid(p_pattern_key_sha256) THEN
    RAISE EXCEPTION 'pattern key is invalid' USING ERRCODE='22023';
  END IF;
  SELECT * INTO head FROM memory.pattern_hypothesis_v5_1
  WHERE owner_user_id=actor AND pattern_key_sha256=p_pattern_key_sha256;
  IF NOT FOUND THEN
    state:=jsonb_build_object(
      'exists',false,'pattern_id',NULL,'revision_number',0,
      'current_revision_id',NULL,'status',NULL,
      'definition_sha256',NULL,'input_manifest_sha256',NULL
    );
  ELSE
    IF head.current_revision_id IS NOT NULL THEN
      SELECT * INTO STRICT revision
      FROM memory.pattern_hypothesis_revision_v5_1
      WHERE owner_user_id=actor AND pattern_id=head.pattern_id
        AND revision_id=head.current_revision_id;
    END IF;
    state:=jsonb_build_object(
      'exists',true,'pattern_id',head.pattern_id::text,
      'revision_number',head.revision_number,
      'current_revision_id',head.current_revision_id::text,
      'status',head.status::text,
      'definition_sha256',revision.definition_sha256,
      'input_manifest_sha256',revision.input_manifest_sha256
    );
  END IF;
  RETURN state||jsonb_build_object(
    'head_state_sha256',memory.v5_digest_text(memory.v5_canonical_json_text(state))
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_pattern_review_v5_1(
  p_proposal jsonb,
  p_decision memory.pattern_review_decision_v5_1,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_review_reason_codes jsonb
)
RETURNS TABLE(
  pattern_key_sha256 text,
  proposal_sha256 text,
  expected_pattern_id uuid,
  expected_revision_number integer,
  expected_head_state_sha256 text,
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
  expected_key text;
  observed_count integer;
  occurrence_count integer;
  counterexample_count integer;
  independent_count integer;
  temporal_count integer;
  maximum_sensitivity_rank integer;
  input_manifest text;
  is_trusted_self boolean;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF NOT memory.pattern_proposal_shape_valid_v5_1(p_proposal)
     OR p_decision IS NULL
     OR p_reviewer_type NOT IN ('user','admin','system')
     OR btrim(COALESCE(p_reviewer_ref,''))=''
     OR length(p_reviewer_ref)>500
     OR NOT memory.v5_reason_codes_valid(p_review_reason_codes,32)
     OR jsonb_array_length(p_review_reason_codes)<1 THEN
    RAISE EXCEPTION 'pattern review inputs are invalid' USING ERRCODE='22023';
  END IF;
  IF p_reviewer_type IN ('user','admin') AND p_reviewer_ref<>actor::text THEN
    RAISE EXCEPTION 'human pattern reviewer must equal the actor owner'
      USING ERRCODE='42501';
  END IF;

  expected_key:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_pattern_key_v5_1',p_proposal->>'pattern_kind',
    p_proposal->>'subject_entity_id',
    COALESCE(p_proposal->>'secondary_entity_id',''),
    p_proposal->>'predicate_family'
  ));
  IF expected_key<>p_proposal->>'pattern_key_sha256' THEN
    RAISE EXCEPTION 'pattern key does not match stable identity fields'
      USING ERRCODE='23514';
  END IF;

  SELECT entity.entity_key='self'
         AND entity.entity_type='self'
         AND entity.metadata->>'identity_state'='trusted_owner_self'
    INTO is_trusted_self
  FROM memory.entity AS entity
  WHERE entity.owner_user_id=actor
    AND entity.entity_id=(p_proposal->>'subject_entity_id')::uuid
    AND entity.status='active';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'pattern subject is absent, inactive, or cross-owner'
      USING ERRCODE='23514';
  END IF;
  IF p_proposal->'secondary_entity_id'<>'null'::jsonb
     AND NOT EXISTS (
       SELECT 1 FROM memory.entity AS entity
       WHERE entity.owner_user_id=actor
         AND entity.entity_id=(p_proposal->>'secondary_entity_id')::uuid
         AND entity.status='active'
     ) THEN
    RAISE EXCEPTION 'pattern secondary entity is absent, inactive, or cross-owner'
      USING ERRCODE='23514';
  END IF;

  WITH items AS (
    SELECT value AS item
    FROM jsonb_array_elements(p_proposal->'observation_items')
  ), checked AS (
    SELECT item,observation.observation_id,observation.evidence_id,
           observation.observation_sha256,evidence.content_sha256,
           binding.subject_entity_id,binding.object_entity_id,
           observation.sensitivity,
           observation.predicate
    FROM items
    JOIN memory.observation AS observation
      ON observation.owner_user_id=actor
     AND observation.observation_id=(item->>'observation_id')::uuid
     AND observation.evidence_id=(item->>'evidence_id')::uuid
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=actor
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=actor
     AND binding.observation_id=observation.observation_id
    WHERE observation.observation_sha256=item->>'observation_sha256'
      AND evidence.content_sha256=item->>'evidence_content_sha256'
      AND binding.subject_entity_id=(p_proposal->>'subject_entity_id')::uuid
      AND (
        p_proposal->'secondary_entity_id'='null'::jsonb
        OR binding.object_entity_id=(p_proposal->>'secondary_entity_id')::uuid
      )
      AND (
        observation.predicate=p_proposal->>'predicate_family'
        OR observation.predicate LIKE (p_proposal->>'predicate_family')||'.%'
      )
  )
  SELECT count(*),
         count(*) FILTER (WHERE item->>'observation_role'='occurrence'),
         count(*) FILTER (WHERE item->>'observation_role'='counterexample'),
         count(DISTINCT item->>'independence_key_sha256') FILTER (
           WHERE item->>'observation_role'='occurrence'
         ),
         count(DISTINCT item->>'temporal_bucket_sha256') FILTER (
           WHERE item->>'observation_role'='occurrence'
         ),
         max(CASE (checked.sensitivity::text)
           WHEN 'low' THEN 1 WHEN 'medium' THEN 2
           WHEN 'high' THEN 3 WHEN 'restricted' THEN 4 END),
         memory.v5_digest_text(COALESCE(string_agg(concat_ws('|',
           item->>'observation_id',item->>'evidence_id',
           item->>'observation_role',item->>'episode_key_sha256',
           item->>'independence_key_sha256',item->>'temporal_bucket_sha256',
           item->>'observation_sha256',item->>'evidence_content_sha256'
         ),E'\n' ORDER BY item->>'observation_id'),'none'))
    INTO observed_count,occurrence_count,counterexample_count,
         independent_count,temporal_count,maximum_sensitivity_rank,
         input_manifest
  FROM checked;

  IF observed_count<>jsonb_array_length(p_proposal->'observation_items')
     OR observed_count<>(
       SELECT count(DISTINCT value->>'observation_id')
       FROM jsonb_array_elements(p_proposal->'observation_items')
     )
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_proposal->'observation_items')
       WHERE value->>'observation_role' IN ('occurrence','counterexample')
       GROUP BY value->>'observation_role',value->>'episode_key_sha256'
       HAVING count(*)>1
     )
     OR occurrence_count<>(p_proposal->>'occurrence_count')::integer
     OR counterexample_count<>(p_proposal->>'counterexample_count')::integer
     OR independent_count<>(p_proposal->>'independent_episode_count')::integer
     OR temporal_count<>(p_proposal->>'distinct_temporal_bucket_count')::integer
     OR maximum_sensitivity_rank>(CASE (p_proposal->>'sensitivity')
       WHEN 'low' THEN 1 WHEN 'medium' THEN 2
       WHEN 'high' THEN 3 WHEN 'restricted' THEN 4 END)
     OR input_manifest<>p_proposal->>'input_manifest_sha256' THEN
    RAISE EXCEPTION 'pattern observation manifest is stale or inflated'
      USING ERRCODE='23514';
  END IF;

  IF (p_proposal->>'automatic_review_eligible')::boolean AND NOT (
       p_proposal->>'pattern_kind' IN ('recurrence','persistence')
       AND p_proposal->>'pattern_state' IN ('emerging','supported')
       AND occurrence_count>=3 AND independent_count>=2 AND temporal_count>=2
       AND counterexample_count=0 AND is_trusted_self
       AND p_proposal->'secondary_entity_id'='null'::jsonb
     ) THEN
    RAISE EXCEPTION 'proposal overstates automatic review eligibility'
      USING ERRCODE='23514';
  END IF;
  IF p_reviewer_type='system' AND p_decision='authorized' AND NOT (
       (p_proposal->>'automatic_review_eligible')::boolean
       AND p_proposal->>'pattern_kind' IN ('recurrence','persistence')
     ) THEN
    RAISE EXCEPTION 'system cannot authorize this pattern class'
      USING ERRCODE='42501';
  END IF;

  state:=memory.pattern_head_state_v5_1(p_proposal->>'pattern_key_sha256');
  IF (state->>'exists')::boolean AND EXISTS (
    SELECT 1 FROM memory.pattern_hypothesis_v5_1 AS head
    WHERE head.owner_user_id=actor
      AND head.pattern_key_sha256=p_proposal->>'pattern_key_sha256'
      AND (
        head.pattern_kind::text<>p_proposal->>'pattern_kind'
        OR head.subject_entity_id<>(p_proposal->>'subject_entity_id')::uuid
        OR head.secondary_entity_id IS DISTINCT FROM
             CASE WHEN p_proposal->'secondary_entity_id'='null'::jsonb
               THEN NULL ELSE (p_proposal->>'secondary_entity_id')::uuid END
        OR head.predicate_family<>p_proposal->>'predicate_family'
        OR head.sensitivity::text<>p_proposal->>'sensitivity'
      )
  ) THEN
    RAISE EXCEPTION 'pattern stable identity fields conflict with existing head'
      USING ERRCODE='23514';
  END IF;

  pattern_key_sha256:=p_proposal->>'pattern_key_sha256';
  proposal_sha256:=p_proposal->>'proposal_sha256';
  expected_pattern_id:=NULLIF(state->>'pattern_id','')::uuid;
  expected_revision_number:=(state->>'revision_number')::integer;
  expected_head_state_sha256:=state->>'head_state_sha256';
  authorization_manifest_sha256:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_pattern_review_authorization_v5_1',actor::text,
    pattern_key_sha256,proposal_sha256,p_decision::text,p_reviewer_type,
    p_reviewer_ref,memory.v5_canonical_json_text(p_review_reason_codes),
    expected_revision_number::text,expected_head_state_sha256
  ));
  RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_pattern_v5_1(
  p_request_id uuid,
  p_proposal jsonb,
  p_decision memory.pattern_review_decision_v5_1,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_review_reason_codes jsonb,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(review_id uuid,outcome text,observations_recorded integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  replayed memory.pattern_operation_request_v5_1%ROWTYPE;
  existing memory.pattern_review_v5_1%ROWTYPE;
  review_id_value uuid:=gen_random_uuid();
  observation_count integer;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF p_request_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'pattern review request and manifest are required'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    actor::text,'pattern_review',p_proposal->>'pattern_key_sha256'
  ),0));

  SELECT * INTO replayed FROM memory.pattern_operation_request_v5_1
  WHERE owner_user_id=actor AND request_id=p_request_id;
  IF FOUND THEN
    IF replayed.operation<>'review_pattern'
       OR replayed.manifest_sha256<>p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'pattern request replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT (replayed.result->>'review_id')::uuid,'replayed',0;
    RETURN;
  END IF;

  SELECT * INTO preflight FROM memory.preflight_pattern_review_v5_1(
    p_proposal,p_decision,p_reviewer_type,p_reviewer_ref,p_review_reason_codes
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'pattern review authorization manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO existing FROM memory.pattern_review_v5_1
  WHERE owner_user_id=actor
    AND pattern_key_sha256=preflight.pattern_key_sha256
    AND authorization_manifest_sha256=p_authorization_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT existing.review_id,'replayed',0;
    RETURN;
  END IF;

  INSERT INTO memory.pattern_review_v5_1(
    review_id,owner_user_id,pattern_key_sha256,proposal,proposal_sha256,
    decision,reviewer_type,reviewer_ref,review_reason_codes,
    expected_pattern_id,expected_revision_number,expected_head_state_sha256,
    authorization_manifest_sha256
  ) VALUES (
    review_id_value,actor,preflight.pattern_key_sha256,p_proposal,
    preflight.proposal_sha256,p_decision,p_reviewer_type,p_reviewer_ref,
    p_review_reason_codes,preflight.expected_pattern_id,
    preflight.expected_revision_number,preflight.expected_head_state_sha256,
    p_authorization_manifest_sha256
  );
  INSERT INTO memory.pattern_review_observation_v5_1(
    owner_user_id,review_id,observation_id,evidence_id,observation_role,
    episode_key_sha256,independence_key_sha256,temporal_bucket_sha256,
    observation_sha256,evidence_content_sha256
  )
  SELECT actor,review_id_value,(item->>'observation_id')::uuid,
    (item->>'evidence_id')::uuid,
    (item->>'observation_role')::memory.pattern_observation_role_v5_1,
    item->>'episode_key_sha256',item->>'independence_key_sha256',
    item->>'temporal_bucket_sha256',item->>'observation_sha256',
    item->>'evidence_content_sha256'
  FROM jsonb_array_elements(p_proposal->'observation_items') AS item;
  GET DIAGNOSTICS observation_count=ROW_COUNT;
  result_value:=jsonb_build_object(
    'review_id',review_id_value::text,
    'pattern_key_sha256',preflight.pattern_key_sha256,
    'decision',p_decision::text,'observations_recorded',observation_count
  );
  INSERT INTO memory.pattern_operation_request_v5_1(
    owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
  ) VALUES (
    actor,p_request_id,'review_pattern',p_authorization_manifest_sha256,
    result_value,session_user
  );
  RETURN QUERY SELECT review_id_value,'applied',observation_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_pattern_apply_v5_1(
  p_review_id uuid
)
RETURNS TABLE(
  review_id uuid,
  pattern_id uuid,
  prior_revision_number integer,
  proposal_sha256 text,
  current_head_state_sha256 text,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  review memory.pattern_review_v5_1%ROWTYPE;
  current_preflight record;
  state jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  SELECT stored.* INTO review FROM memory.pattern_review_v5_1 AS stored
  WHERE stored.owner_user_id=actor AND stored.review_id=p_review_id;
  IF NOT FOUND OR review.decision<>'authorized' THEN
    RAISE EXCEPTION 'authorized pattern review is required'
      USING ERRCODE='23514';
  END IF;
  IF review.review_id IS DISTINCT FROM (
    SELECT latest.review_id FROM memory.pattern_review_v5_1 AS latest
    WHERE latest.owner_user_id=actor
      AND latest.pattern_key_sha256=review.pattern_key_sha256
    ORDER BY latest.created_at DESC,latest.review_id DESC LIMIT 1
  ) THEN
    RAISE EXCEPTION 'pattern review is not the latest decision'
      USING ERRCODE='23514';
  END IF;
  SELECT * INTO current_preflight FROM memory.preflight_pattern_review_v5_1(
    review.proposal,review.decision,review.reviewer_type,
    review.reviewer_ref,review.review_reason_codes
  );
  IF current_preflight.authorization_manifest_sha256
       <>review.authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'pattern review is stale'
      USING ERRCODE='23514';
  END IF;
  state:=memory.pattern_head_state_v5_1(review.pattern_key_sha256);
  review_id:=review.review_id;
  pattern_id:=NULLIF(state->>'pattern_id','')::uuid;
  prior_revision_number:=(state->>'revision_number')::integer;
  proposal_sha256:=review.proposal_sha256;
  current_head_state_sha256:=state->>'head_state_sha256';
  apply_manifest_sha256:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_pattern_apply_v5_1',actor::text,review.review_id::text,
    review.pattern_key_sha256,review.proposal_sha256,
    review.authorization_manifest_sha256,prior_revision_number::text,
    current_head_state_sha256
  ));
  RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_pattern_head_update_v5_1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
BEGIN
  IF current_user<>'memory_v5_epistemic_writer'
     OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.pattern_id IS DISTINCT FROM OLD.pattern_id
     OR NEW.pattern_key_sha256 IS DISTINCT FROM OLD.pattern_key_sha256
     OR NEW.pattern_kind IS DISTINCT FROM OLD.pattern_kind
     OR NEW.subject_entity_id IS DISTINCT FROM OLD.subject_entity_id
     OR NEW.secondary_entity_id IS DISTINCT FROM OLD.secondary_entity_id
     OR NEW.predicate_family IS DISTINCT FROM OLD.predicate_family
     OR NEW.sensitivity IS DISTINCT FROM OLD.sensitivity
     OR NEW.status IS DISTINCT FROM OLD.status
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
     OR NEW.revision_number<>OLD.revision_number+1
     OR NEW.current_revision_id IS NULL
     OR NEW.current_revision_id IS NOT DISTINCT FROM OLD.current_revision_id
     OR NEW.updated_at<OLD.updated_at
     OR NOT EXISTS (
       SELECT 1 FROM memory.pattern_hypothesis_revision_v5_1 AS revision
       WHERE revision.owner_user_id=NEW.owner_user_id
         AND revision.pattern_id=NEW.pattern_id
         AND revision.revision_id=NEW.current_revision_id
         AND revision.revision_number=NEW.revision_number
     ) THEN
    RAISE EXCEPTION 'pattern head update violates controlled apply boundary'
      USING ERRCODE='42501';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS pattern_hypothesis_v5_1_controlled_update
  ON memory.pattern_hypothesis_v5_1;
CREATE TRIGGER pattern_hypothesis_v5_1_controlled_update
BEFORE UPDATE ON memory.pattern_hypothesis_v5_1
FOR EACH ROW EXECUTE FUNCTION memory.guard_pattern_head_update_v5_1();

CREATE OR REPLACE FUNCTION memory.apply_pattern_review_v5_1(
  p_request_id uuid,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  event_id uuid,
  outcome text,
  pattern_id uuid,
  pattern_revision_id uuid,
  resulting_revision_number integer,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  review memory.pattern_review_v5_1%ROWTYPE;
  replayed memory.pattern_operation_request_v5_1%ROWTYPE;
  applied memory.pattern_apply_event_v5_1%ROWTYPE;
  preflight record;
  head memory.pattern_hypothesis_v5_1%ROWTYPE;
  pattern_id_value uuid;
  revision_id_value uuid:=gen_random_uuid();
  event_id_value uuid:=gen_random_uuid();
  revision_number_value integer;
  link_count integer;
  head_insert_count integer:=0;
  definition_sha text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF p_request_id IS NULL OR p_review_id IS NULL
     OR NOT memory.v5_sha256_valid(p_apply_manifest_sha256) THEN
    RAISE EXCEPTION 'pattern apply identifiers are invalid' USING ERRCODE='22023';
  END IF;
  SELECT * INTO review FROM memory.pattern_review_v5_1
  WHERE owner_user_id=actor AND review_id=p_review_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'pattern review is absent or cross-owner'
      USING ERRCODE='23514';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    actor::text,'pattern_apply',review.pattern_key_sha256
  ),0));

  SELECT * INTO replayed FROM memory.pattern_operation_request_v5_1
  WHERE owner_user_id=actor AND request_id=p_request_id;
  IF FOUND THEN
    IF replayed.operation<>'apply_pattern'
       OR replayed.manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'pattern request replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replayed.result->>'event_id')::uuid,'replayed',
      (replayed.result->>'pattern_id')::uuid,
      (replayed.result->>'pattern_revision_id')::uuid,
      (replayed.result->>'resulting_revision_number')::integer,0;
    RETURN;
  END IF;
  SELECT * INTO applied FROM memory.pattern_apply_event_v5_1
  WHERE owner_user_id=actor AND review_id=p_review_id;
  IF FOUND THEN
    IF applied.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'pattern review was applied under a different manifest'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT applied.event_id,'replayed',applied.pattern_id,
      applied.pattern_revision_id,applied.resulting_revision_number,0;
    RETURN;
  END IF;

  PERFORM 1 FROM memory.pattern_hypothesis_v5_1
  WHERE owner_user_id=actor AND pattern_key_sha256=review.pattern_key_sha256
  FOR UPDATE;
  SELECT * INTO preflight FROM memory.preflight_pattern_apply_v5_1(p_review_id);
  IF preflight.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'pattern apply manifest mismatch' USING ERRCODE='23514';
  END IF;

  SELECT * INTO head FROM memory.pattern_hypothesis_v5_1
  WHERE owner_user_id=actor AND pattern_key_sha256=review.pattern_key_sha256;
  IF NOT FOUND THEN
    pattern_id_value:=gen_random_uuid();
    INSERT INTO memory.pattern_hypothesis_v5_1(
      pattern_id,owner_user_id,pattern_key_sha256,pattern_kind,
      subject_entity_id,secondary_entity_id,predicate_family,sensitivity
    ) VALUES (
      pattern_id_value,actor,review.pattern_key_sha256,
      (review.proposal->>'pattern_kind')::memory.pattern_kind_v5_1,
      (review.proposal->>'subject_entity_id')::uuid,
      CASE WHEN review.proposal->'secondary_entity_id'='null'::jsonb
        THEN NULL ELSE (review.proposal->>'secondary_entity_id')::uuid END,
      review.proposal->>'predicate_family',
      (review.proposal->>'sensitivity')::memory.sensitivity_level
    );
    head_insert_count:=1;
    revision_number_value:=1;
  ELSE
    pattern_id_value:=head.pattern_id;
    revision_number_value:=head.revision_number+1;
  END IF;
  definition_sha:=memory.v5_digest_text(concat_ws('|',
    review.pattern_key_sha256,review.proposal->>'pattern_kind',
    review.proposal->>'pattern_state',review.proposal->>'occurrence_count',
    review.proposal->>'counterexample_count',
    review.proposal->>'independent_episode_count',
    review.proposal->>'distinct_temporal_bucket_count',
    COALESCE(review.proposal->>'valid_from',''),
    COALESCE(review.proposal->>'valid_to',''),
    review.proposal->>'automatic_review_eligible',
    memory.v5_canonical_json_text(review.proposal->'reason_codes')
  ));
  INSERT INTO memory.pattern_hypothesis_revision_v5_1(
    revision_id,owner_user_id,pattern_id,revision_number,pattern_state,
    occurrence_count,counterexample_count,independent_episode_count,
    distinct_temporal_bucket_count,valid_from,valid_to,
    automatic_review_eligible,identity_inference_forbidden,
    causal_inference_forbidden,reason_codes,definition_sha256,
    input_manifest_sha256,prior_revision_id,method_version
  ) VALUES (
    revision_id_value,actor,pattern_id_value,revision_number_value,
    (review.proposal->>'pattern_state')::memory.pattern_state_v5_1,
    (review.proposal->>'occurrence_count')::integer,
    (review.proposal->>'counterexample_count')::integer,
    (review.proposal->>'independent_episode_count')::integer,
    (review.proposal->>'distinct_temporal_bucket_count')::integer,
    NULLIF(review.proposal->>'valid_from','')::date,
    NULLIF(review.proposal->>'valid_to','')::date,
    (review.proposal->>'automatic_review_eligible')::boolean,true,true,
    review.proposal->'reason_codes',definition_sha,
    review.proposal->>'input_manifest_sha256',head.current_revision_id,
    review.proposal->>'method_version'
  );
  INSERT INTO memory.pattern_observation_link_v5_1(
    owner_user_id,pattern_id,pattern_revision_id,observation_id,
    observation_role,episode_key_sha256,independence_key_sha256,
    temporal_bucket_sha256,observation_sha256,evidence_content_sha256
  ) SELECT owner_user_id,pattern_id_value,revision_id_value,observation_id,
    observation_role,episode_key_sha256,independence_key_sha256,
    temporal_bucket_sha256,observation_sha256,evidence_content_sha256
  FROM memory.pattern_review_observation_v5_1
  WHERE owner_user_id=actor AND review_id=p_review_id;
  GET DIAGNOSTICS link_count=ROW_COUNT;
  UPDATE memory.pattern_hypothesis_v5_1 AS target SET
    current_revision_id=revision_id_value,
    revision_number=revision_number_value,
    updated_at=clock_timestamp()
  WHERE target.owner_user_id=actor AND target.pattern_id=pattern_id_value;

  INSERT INTO memory.pattern_apply_event_v5_1(
    event_id,owner_user_id,request_id,review_id,pattern_id,
    prior_revision_number,resulting_revision_number,pattern_revision_id,
    apply_manifest_sha256,invoked_by_session
  ) VALUES (
    event_id_value,actor,p_request_id,p_review_id,pattern_id_value,
    revision_number_value-1,revision_number_value,revision_id_value,
    p_apply_manifest_sha256,session_user
  );
  result_value:=jsonb_build_object(
    'event_id',event_id_value::text,'pattern_id',pattern_id_value::text,
    'pattern_revision_id',revision_id_value::text,
    'resulting_revision_number',revision_number_value
  );
  INSERT INTO memory.pattern_operation_request_v5_1(
    owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
  ) VALUES (
    actor,p_request_id,'apply_pattern',p_apply_manifest_sha256,
    result_value,session_user
  );
  RETURN QUERY SELECT event_id_value,'applied',pattern_id_value,
    revision_id_value,revision_number_value,
    head_insert_count+link_count+3;
END
$function$;

GRANT SELECT ON memory.observation_entity_binding
  TO memory_v5_epistemic_writer;
GRANT SELECT,INSERT ON
  memory.pattern_review_v5_1,
  memory.pattern_review_observation_v5_1,
  memory.pattern_apply_event_v5_1,
  memory.pattern_operation_request_v5_1
TO memory_v5_epistemic_writer;
GRANT INSERT,UPDATE ON memory.pattern_hypothesis_v5_1
  TO memory_v5_epistemic_writer;
GRANT INSERT ON
  memory.pattern_hypothesis_revision_v5_1,
  memory.pattern_observation_link_v5_1
TO memory_v5_epistemic_writer;
GRANT USAGE ON TYPE memory.pattern_review_decision_v5_1
  TO memory_v5_epistemic_writer;

ALTER FUNCTION memory.pattern_proposal_sha256_v5_1(jsonb)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.pattern_proposal_shape_valid_v5_1(jsonb)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.pattern_head_state_v5_1(text)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.preflight_pattern_review_v5_1(
  jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb
) OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.review_pattern_v5_1(
  uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text
) OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.preflight_pattern_apply_v5_1(uuid)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.guard_pattern_head_update_v5_1()
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.apply_pattern_review_v5_1(uuid,uuid,text)
  OWNER TO memory_v5_epistemic_writer;

REVOKE ALL ON FUNCTION memory.pattern_proposal_sha256_v5_1(jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.pattern_proposal_shape_valid_v5_1(jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.pattern_head_state_v5_1(text)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_pattern_review_v5_1(
  jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.review_pattern_v5_1(
  uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_pattern_apply_v5_1(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.guard_pattern_head_update_v5_1()
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.apply_pattern_review_v5_1(uuid,uuid,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_pattern_review_v5_1(
  jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_pattern_v5_1(
  uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_pattern_apply_v5_1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_pattern_review_v5_1(uuid,uuid,text)
  TO brains_app;

COMMIT;
