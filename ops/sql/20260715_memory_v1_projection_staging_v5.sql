BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection staging V5 migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM pg_roles
       WHERE rolname = 'memory_v5_writer'
         AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
         AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
     ) THEN
    RAISE EXCEPTION 'restricted memory_v5_writer role is required';
  END IF;
  IF to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.claim_revision') IS NULL
     OR to_regclass('memory.user_preference') IS NULL
     OR to_regclass('memory.preference_revision') IS NULL
     OR to_regclass('memory.project_space') IS NULL
     OR to_regclass('memory.project_knowledge_head') IS NULL
     OR to_regclass('memory.project_knowledge_revision') IS NULL THEN
    RAISE EXCEPTION
      'V5 observation/writer and durable claim/preference/project schemas are required';
  END IF;
  IF to_regprocedure('memory.v5_jsonb_exact_keys(jsonb,text[])') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL THEN
    RAISE EXCEPTION 'memory V5 writer helpers are required';
  END IF;
END
$block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_lane_v5 AS ENUM (
    'claim', 'preference', 'project_knowledge'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_target_action_v5 AS ENUM (
    'create', 'reinforce', 'revise', 'relate', 'defer', 'reject'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_review_state_v5 AS ENUM (
    'auto_apply_eligible', 'manual_review_required', 'deferred', 'rejected'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_observation_stance_v5 AS ENUM (
    'supports', 'opposes', 'context', 'correction_target'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_temporal_materialization_v5 AS ENUM (
    'link_only', 'state_validity_only'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_relation_type_v5 AS ENUM (
    'contradicts', 'supersedes', 'corrects',
    'qualifies', 'depends_on', 'derived_from'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.projection_review_decision_v5 AS ENUM (
    'authorized', 'rejected', 'deferred'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

CREATE OR REPLACE FUNCTION memory.v5_canonical_json_text(value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  result text;
BEGIN
  CASE jsonb_typeof(value)
    WHEN 'object' THEN
      SELECT '{' || COALESCE(string_agg(
        to_jsonb(entry.key)::text || ':' ||
          memory.v5_canonical_json_text(entry.value),
        ',' ORDER BY entry.key
      ), '') || '}'
      INTO result
      FROM jsonb_each(value) AS entry(key, value);
      RETURN result;
    WHEN 'array' THEN
      SELECT '[' || COALESCE(string_agg(
        memory.v5_canonical_json_text(entry.value),
        ',' ORDER BY entry.ordinality
      ), '') || ']'
      INTO result
      FROM jsonb_array_elements(value) WITH ORDINALITY
        AS entry(value, ordinality);
      RETURN result;
    ELSE
      RETURN value::text;
  END CASE;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_projection_owner_manifest_sha256(
  owner_user_id uuid,
  packet_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'manifest_version', 'memory_projection_owner_manifest_v5',
      'owner_user_id', owner_user_id::text,
      'packet_sha256', packet_sha256
    )
  ))
$function$;

CREATE OR REPLACE FUNCTION memory.v5_projection_semantic_key_sha256(
  owner_user_id uuid,
  lane memory.projection_lane_v5,
  subject_entity_id uuid,
  predicate text,
  object_kind text,
  object_entity_id uuid,
  object_literal_sha256 text,
  polarity memory.observation_polarity,
  modality memory.observation_modality,
  lane_scope jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'identity_version', 'memory_projection_identity_v5',
      'owner_user_id', owner_user_id::text,
      'lane', lane::text,
      'subject_entity_id', subject_entity_id::text,
      'predicate', predicate,
      'object_kind', object_kind,
      'object_entity_id', object_entity_id::text,
      'object_literal_sha256', object_literal_sha256,
      'polarity', polarity::text,
      'modality', modality::text,
      'lane_scope', lane_scope
    )
  ))
$function$;

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.observation'::regclass
      AND conname = 'observation_owner_id_sha_uq'
  ) THEN
    ALTER TABLE memory.observation
      ADD CONSTRAINT observation_owner_id_sha_uq
      UNIQUE (owner_user_id, observation_id, observation_sha256);
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.projection_plan (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL DEFAULT gen_random_uuid(),
  contract_version text NOT NULL,
  predicate_registry_version text NOT NULL,
  projection_policy_version text NOT NULL,
  projector text NOT NULL,
  projector_version text NOT NULL,
  packet_text text NOT NULL,
  packet_text_sha256 text NOT NULL,
  packet_sha256 text NOT NULL,
  owner_manifest_sha256 text NOT NULL,
  projection_count integer NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id),
  UNIQUE (owner_user_id, packet_sha256),
  UNIQUE (owner_user_id, owner_manifest_sha256),
  UNIQUE (owner_user_id, plan_id, predicate_registry_version),
  CHECK (contract_version = 'memory_v1_projection_plan_v5'),
  CHECK (predicate_registry_version = 'memory_predicate_registry_v5'),
  CHECK (projection_policy_version = 'memory_projection_policy_v5'),
  CHECK (btrim(projector) <> '' AND length(projector) <= 120),
  CHECK (btrim(projector_version) <> '' AND length(projector_version) <= 120),
  CHECK (octet_length(packet_text) <= 1048576),
  CHECK (memory.v5_sha256_valid(packet_text_sha256)),
  CHECK (memory.v5_sha256_valid(packet_sha256)),
  CHECK (memory.v5_sha256_valid(owner_manifest_sha256)),
  CHECK (memory.v5_digest_text(packet_text) = packet_text_sha256),
  CHECK (projection_count BETWEEN 0 AND 100),
  CHECK (jsonb_typeof(packet_text::jsonb) = 'object'),
  CHECK (memory.v5_jsonb_exact_keys(packet_text::jsonb, ARRAY[
    'contract_version', 'predicate_registry_version',
    'projection_policy_version', 'projector', 'projector_version',
    'projections', 'packet_sha256'
  ])),
  CHECK ((packet_text::jsonb)->>'contract_version' = contract_version),
  CHECK ((packet_text::jsonb)->>'predicate_registry_version' = predicate_registry_version),
  CHECK ((packet_text::jsonb)->>'projection_policy_version' = projection_policy_version),
  CHECK ((packet_text::jsonb)->>'projector' = projector),
  CHECK ((packet_text::jsonb)->>'projector_version' = projector_version),
  CHECK (jsonb_typeof((packet_text::jsonb)->'projections') = 'array'),
  CHECK (jsonb_array_length((packet_text::jsonb)->'projections') = projection_count),
  CHECK ((packet_text::jsonb)->>'packet_sha256' = packet_sha256),
  CHECK (
    memory.v5_digest_text(memory.v5_canonical_json_text(
      (packet_text::jsonb) - 'packet_sha256'
    )) = packet_sha256
  ),
  CHECK (
    memory.v5_projection_owner_manifest_sha256(owner_user_id, packet_sha256)
      = owner_manifest_sha256
  )
);

CREATE INDEX IF NOT EXISTS projection_plan_owner_time_idx
  ON memory.projection_plan(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory.projection_plan_item (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  predicate_registry_version text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL,
  projection jsonb NOT NULL,
  projection_sha256 text NOT NULL,
  subject_entity_id uuid NOT NULL,
  predicate text NOT NULL,
  object_kind text NOT NULL,
  object_entity_id uuid,
  object_literal_sha256 text,
  polarity memory.observation_polarity NOT NULL,
  modality memory.observation_modality NOT NULL,
  lane_scope jsonb NOT NULL,
  semantic_key_sha256 text NOT NULL,
  target_action memory.projection_target_action_v5 NOT NULL,
  expected_revision_number integer,
  target_reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  temporal_materialization memory.projection_temporal_materialization_v5 NOT NULL,
  temporal_source_observation_id uuid,
  review_state memory.projection_review_state_v5 NOT NULL,
  authorization_required boolean NOT NULL,
  review_reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref),
  UNIQUE (owner_user_id, plan_id, projection_sha256),
  UNIQUE (
    owner_user_id, plan_id, projection_ref,
    lane, target_action, review_state
  ),
  UNIQUE (
    owner_user_id, plan_id, projection_ref,
    lane, target_action
  ),
  UNIQUE (
    owner_user_id, plan_id, projection_ref,
    lane, review_state
  ),
  UNIQUE (
    owner_user_id, plan_id, projection_ref,
    review_state
  ),
  UNIQUE (
    owner_user_id, plan_id, projection_ref,
    projection_sha256, semantic_key_sha256
  ),
  FOREIGN KEY (owner_user_id, plan_id, predicate_registry_version)
    REFERENCES memory.projection_plan(
      owner_user_id, plan_id, predicate_registry_version
    ) ON DELETE RESTRICT,
  FOREIGN KEY (predicate, predicate_registry_version)
    REFERENCES memory.predicate_contract(predicate, registry_version)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, subject_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, object_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CHECK (projection_ref ~ '^p[0-9]{2}$'),
  CHECK (predicate_registry_version = 'memory_predicate_registry_v5'),
  CHECK (jsonb_typeof(projection) = 'object' AND pg_column_size(projection) <= 65536),
  CHECK (memory.v5_jsonb_exact_keys(projection, ARRAY[
    'projection_ref', 'lane', 'observation_inputs', 'identity', 'target',
    'temporal_policy', 'review', 'relations', 'payload'
  ])),
  CHECK (projection->>'projection_ref' = projection_ref),
  CHECK (projection->>'lane' = lane::text),
  CHECK (memory.v5_sha256_valid(projection_sha256)),
  CHECK (
    memory.v5_digest_text(memory.v5_canonical_json_text(projection))
      = projection_sha256
  ),
  CHECK (object_kind IN ('entity', 'literal')),
  CHECK (
    (object_kind = 'entity'
     AND object_entity_id IS NOT NULL AND object_literal_sha256 IS NULL)
    OR
    (object_kind = 'literal'
     AND object_entity_id IS NULL
     AND memory.v5_sha256_valid(object_literal_sha256))
  ),
  CHECK (jsonb_typeof(lane_scope) = 'object' AND pg_column_size(lane_scope) <= 16384),
  CHECK (memory.v5_sha256_valid(semantic_key_sha256)),
  CHECK (
    memory.v5_projection_semantic_key_sha256(
      owner_user_id, lane, subject_entity_id, predicate,
      object_kind, object_entity_id, object_literal_sha256,
      polarity, modality, lane_scope
    ) = semantic_key_sha256
  ),
  CHECK (
    (target_action IN ('create', 'defer', 'reject')
     AND expected_revision_number IS NULL)
    OR
    (target_action IN ('reinforce', 'revise', 'relate')
     AND expected_revision_number > 0)
  ),
  CHECK (memory.v5_reason_codes_valid(target_reason_codes, 20)),
  CHECK (memory.v5_reason_codes_valid(review_reason_codes, 20)),
  CHECK (
    (review_state = 'auto_apply_eligible'
     AND NOT authorization_required
     AND target_action IN ('create', 'reinforce'))
    OR
    (review_state = 'manual_review_required'
     AND authorization_required
     AND target_action IN ('create', 'reinforce', 'revise', 'relate'))
    OR
    (review_state = 'deferred'
     AND NOT authorization_required AND target_action = 'defer')
    OR
    (review_state = 'rejected'
     AND NOT authorization_required AND target_action = 'reject')
  ),
  CHECK (
    (temporal_materialization = 'link_only')
    OR
    (temporal_materialization = 'state_validity_only'
     AND lane = 'project_knowledge'
     AND temporal_source_observation_id IS NOT NULL)
  ),
  CHECK (memory.v5_jsonb_exact_keys(projection->'identity', ARRAY[
    'subject_entity_id', 'predicate', 'object_kind', 'object_entity_id',
    'object_literal_sha256', 'polarity', 'modality', 'semantic_key_sha256'
  ])),
  CHECK (projection#>>'{identity,subject_entity_id}' = subject_entity_id::text),
  CHECK (projection#>>'{identity,predicate}' = predicate),
  CHECK (projection#>>'{identity,object_kind}' = object_kind),
  CHECK (
    NULLIF(projection#>>'{identity,object_entity_id}', '')
      IS NOT DISTINCT FROM object_entity_id::text
  ),
  CHECK (
    NULLIF(projection#>>'{identity,object_literal_sha256}', '')
      IS NOT DISTINCT FROM object_literal_sha256
  ),
  CHECK (projection#>>'{identity,polarity}' = polarity::text),
  CHECK (projection#>>'{identity,modality}' = modality::text),
  CHECK (projection#>>'{identity,semantic_key_sha256}' = semantic_key_sha256),
  CHECK (projection#>>'{target,action}' = target_action::text),
  CHECK (
    NULLIF(projection#>>'{target,expected_revision_number}', '')::integer
      IS NOT DISTINCT FROM expected_revision_number
  ),
  CHECK (projection#>'{target,reason_codes}' = target_reason_codes),
  CHECK (
    projection#>>'{temporal_policy,canonical_source}'
      = 'memory.observation_temporal'
  ),
  CHECK (
    projection#>>'{temporal_policy,materialization}'
      = temporal_materialization::text
  ),
  CHECK (
    NULLIF(projection#>>'{temporal_policy,source_observation_id}', '')
      IS NOT DISTINCT FROM temporal_source_observation_id::text
  ),
  CHECK (projection#>>'{review,state}' = review_state::text),
  CHECK (
    (projection#>>'{review,authorization_required}')::boolean
      = authorization_required
  ),
  CHECK (projection#>'{review,reason_codes}' = review_reason_codes),
  CHECK (jsonb_typeof(projection->'observation_inputs') = 'array'),
  CHECK (jsonb_array_length(projection->'observation_inputs') BETWEEN 1 AND 100),
  CHECK (jsonb_typeof(projection->'relations') = 'array'),
  CHECK (jsonb_array_length(projection->'relations') <= 20)
);

CREATE INDEX IF NOT EXISTS projection_item_owner_semantic_idx
  ON memory.projection_plan_item(
    owner_user_id, lane, semantic_key_sha256, created_at DESC
  );
CREATE INDEX IF NOT EXISTS projection_item_owner_state_idx
  ON memory.projection_plan_item(
    owner_user_id, review_state, target_action, created_at DESC
  );

CREATE TABLE IF NOT EXISTS memory.projection_claim_payload (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL DEFAULT 'claim',
  target_action memory.projection_target_action_v5 NOT NULL,
  target_claim_id uuid,
  claim_class text NOT NULL,
  canonical_text text NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  CHECK (lane = 'claim'),
  CHECK (claim_class IN (
    'direct_claim', 'supportive_context', 'correction', 'never_surface'
  )),
  CHECK (btrim(canonical_text) <> '' AND length(canonical_text) <= 2000),
  CHECK (
    (target_action IN ('create', 'defer', 'reject') AND target_claim_id IS NULL)
    OR
    (target_action IN ('reinforce', 'revise', 'relate')
     AND target_claim_id IS NOT NULL)
  ),
  CHECK (
    (claim_class = 'never_surface' AND surface_policy = 'never')
    OR claim_class <> 'never_surface'
  )
);

CREATE TABLE IF NOT EXISTS memory.projection_preference_payload (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL DEFAULT 'preference',
  target_action memory.projection_target_action_v5 NOT NULL,
  target_preference_id uuid,
  preference_class text NOT NULL,
  preference_domain text NOT NULL,
  preference_key text NOT NULL,
  value jsonb NOT NULL,
  preference_polarity text NOT NULL,
  scope text NOT NULL,
  stability text NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_preference_id)
    REFERENCES memory.user_preference(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  CHECK (lane = 'preference'),
  CHECK (preference_class IN ('life', 'response')),
  CHECK (preference_domain ~ '^[a-z][a-z0-9_]{1,63}$'),
  CHECK (preference_key ~ '^[a-z][a-z0-9_.:-]{1,239}$'),
  CHECK (pg_column_size(value) <= 16384),
  CHECK (preference_polarity IN (
    'likes', 'dislikes', 'prefers', 'avoids', 'not_applicable'
  )),
  CHECK (scope = 'user_global'),
  CHECK (stability IN ('tentative', 'contextual', 'stable')),
  CHECK (
    (target_action IN ('create', 'defer', 'reject')
     AND target_preference_id IS NULL)
    OR
    (target_action IN ('reinforce', 'revise', 'relate')
     AND target_preference_id IS NOT NULL)
  ),
  CHECK (
    (preference_class = 'response'
     AND preference_polarity = 'not_applicable'
     AND surface_policy = 'zero_token_control_only')
    OR
    (preference_class = 'life'
     AND preference_polarity <> 'not_applicable'
     AND surface_policy IN (
       'relevant_recommendation_or_explicit_recall', 'never'
     ))
  )
);

CREATE TABLE IF NOT EXISTS memory.projection_project_payload (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL DEFAULT 'project_knowledge',
  target_action memory.projection_target_action_v5 NOT NULL,
  project_id uuid NOT NULL,
  target_knowledge_id uuid,
  knowledge_kind text NOT NULL,
  knowledge_key text NOT NULL,
  canonical_text text NOT NULL,
  document_state text NOT NULL,
  authority_level text NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, lane, target_action
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id)
    REFERENCES memory.project_space(owner_user_id, project_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, target_knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    ) ON DELETE RESTRICT,
  CHECK (lane = 'project_knowledge'),
  CHECK (knowledge_kind IN (
    'constraint', 'current_state', 'proposed_feature', 'requirement'
  )),
  CHECK (knowledge_key ~ '^[a-z][a-z0-9_.:-]{1,239}$'),
  CHECK (btrim(canonical_text) <> '' AND length(canonical_text) <= 4000),
  CHECK (document_state IN (
    'unverified', 'working', 'proposed', 'ratified',
    'historical', 'superseded'
  )),
  CHECK (authority_level IN (
    'user_reported', 'user_ratified', 'approved_spec',
    'system_observed', 'external_reference'
  )),
  CHECK (surface_policy = 'exact_project_scope_only'),
  CHECK (
    (target_action IN ('create', 'defer', 'reject')
     AND target_knowledge_id IS NULL)
    OR
    (target_action IN ('reinforce', 'revise', 'relate')
     AND target_knowledge_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS memory.projection_plan_observation (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  observation_id uuid NOT NULL,
  observation_sha256 text NOT NULL,
  stance memory.projection_observation_stance_v5 NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref, observation_id),
  FOREIGN KEY (owner_user_id, plan_id, projection_ref)
    REFERENCES memory.projection_plan_item(owner_user_id, plan_id, projection_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id, observation_sha256)
    REFERENCES memory.observation(
      owner_user_id, observation_id, observation_sha256
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation_temporal(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation_entity_binding(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(observation_sha256))
);

CREATE INDEX IF NOT EXISTS projection_observation_owner_observation_idx
  ON memory.projection_plan_observation(owner_user_id, observation_id, created_at DESC);

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory.projection_plan_item'::regclass
      AND conname = 'projection_item_temporal_input_fk'
  ) THEN
    ALTER TABLE memory.projection_plan_item
      ADD CONSTRAINT projection_item_temporal_input_fk
      FOREIGN KEY (
        owner_user_id, plan_id, projection_ref, temporal_source_observation_id
      ) REFERENCES memory.projection_plan_observation(
        owner_user_id, plan_id, projection_ref, observation_id
      ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.projection_plan_relation (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  relation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  lane memory.projection_lane_v5 NOT NULL,
  parent_review_state memory.projection_review_state_v5 NOT NULL,
  relation_type memory.projection_relation_type_v5 NOT NULL,
  target_lane memory.projection_lane_v5 NOT NULL,
  target_claim_id uuid,
  target_preference_id uuid,
  target_project_id uuid,
  target_knowledge_id uuid,
  reason_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref, relation_id),
  UNIQUE NULLS NOT DISTINCT (
    owner_user_id, plan_id, projection_ref, relation_type, target_lane,
    target_claim_id, target_preference_id,
    target_project_id, target_knowledge_id
  ),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, lane, parent_review_state
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, lane, review_state
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_preference_id)
    REFERENCES memory.user_preference(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_project_id, target_knowledge_id)
    REFERENCES memory.project_knowledge_head(
      owner_user_id, project_id, knowledge_id
    ) ON DELETE RESTRICT,
  CHECK (parent_review_state = 'manual_review_required'),
  CHECK (lane = target_lane),
  CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,99}$'),
  CHECK (relation_type <> 'corrects' OR lane = 'claim'),
  CHECK (
    (target_lane = 'claim'
     AND target_claim_id IS NOT NULL
     AND target_preference_id IS NULL
     AND target_project_id IS NULL AND target_knowledge_id IS NULL)
    OR
    (target_lane = 'preference'
     AND target_claim_id IS NULL
     AND target_preference_id IS NOT NULL
     AND target_project_id IS NULL AND target_knowledge_id IS NULL)
    OR
    (target_lane = 'project_knowledge'
     AND target_claim_id IS NULL
     AND target_preference_id IS NULL
     AND target_project_id IS NOT NULL AND target_knowledge_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS memory.projection_review (
  owner_user_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  review_id uuid NOT NULL DEFAULT gen_random_uuid(),
  review_number integer NOT NULL,
  parent_review_state memory.projection_review_state_v5 NOT NULL,
  decision memory.projection_review_decision_v5 NOT NULL,
  expected_projection_sha256 text NOT NULL,
  expected_semantic_key_sha256 text NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text,
  reason text NOT NULL,
  reason_codes jsonb NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, plan_id, projection_ref, review_id),
  UNIQUE (owner_user_id, plan_id, projection_ref, review_number),
  UNIQUE (owner_user_id, plan_id, projection_ref, authorization_manifest_sha256),
  UNIQUE (owner_user_id, plan_id, projection_ref, review_id, decision),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref,
    expected_projection_sha256, expected_semantic_key_sha256
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref,
    projection_sha256, semantic_key_sha256
  ) ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref,
    parent_review_state
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, review_state
  ) ON DELETE RESTRICT,
  CHECK (review_number > 0),
  CHECK (parent_review_state = 'manual_review_required'),
  CHECK (reviewer_type IN ('user', 'system', 'job', 'admin')),
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref) <= 500),
  CHECK (btrim(reason) <> '' AND length(reason) <= 2000),
  CHECK (memory.v5_reason_codes_valid(reason_codes, 20)),
  CHECK (memory.v5_sha256_valid(authorization_manifest_sha256))
);

CREATE INDEX IF NOT EXISTS projection_review_owner_item_idx
  ON memory.projection_review(
    owner_user_id, plan_id, projection_ref, review_number DESC
  );

CREATE TABLE IF NOT EXISTS memory.projection_apply_event (
  owner_user_id uuid NOT NULL,
  event_id uuid NOT NULL DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  projection_ref text NOT NULL,
  lane memory.projection_lane_v5 NOT NULL,
  parent_review_state memory.projection_review_state_v5 NOT NULL,
  review_id uuid,
  review_decision memory.projection_review_decision_v5,
  apply_manifest_sha256 text NOT NULL,
  resulting_claim_id uuid,
  resulting_claim_revision_number integer,
  resulting_preference_revision_id uuid,
  resulting_project_id uuid,
  resulting_project_revision_id uuid,
  outcome text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, event_id),
  UNIQUE (owner_user_id, request_id),
  UNIQUE (owner_user_id, apply_manifest_sha256),
  UNIQUE (owner_user_id, plan_id, projection_ref),
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, lane, parent_review_state
  ) REFERENCES memory.projection_plan_item(
    owner_user_id, plan_id, projection_ref, lane, review_state
  ) ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, plan_id, projection_ref, review_id, review_decision
  ) REFERENCES memory.projection_review(
    owner_user_id, plan_id, projection_ref, review_id, decision
  ) ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, resulting_claim_id, resulting_claim_revision_number
  ) REFERENCES memory.claim_revision(
    owner_user_id, claim_id, revision_number
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resulting_preference_revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, resulting_project_id, resulting_project_revision_id
  ) REFERENCES memory.project_knowledge_revision(
    owner_user_id, project_id, revision_id
  ) ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(apply_manifest_sha256)),
  CHECK (outcome = 'applied'),
  CHECK (actor_user_id = owner_user_id),
  CHECK (
    (parent_review_state = 'auto_apply_eligible'
     AND review_id IS NULL AND review_decision IS NULL)
    OR
    (parent_review_state = 'manual_review_required'
     AND review_id IS NOT NULL AND review_decision = 'authorized')
  ),
  CHECK (
    (lane = 'claim'
     AND resulting_claim_id IS NOT NULL
     AND resulting_claim_revision_number > 0
     AND resulting_preference_revision_id IS NULL
     AND resulting_project_id IS NULL
     AND resulting_project_revision_id IS NULL)
    OR
    (lane = 'preference'
     AND resulting_claim_id IS NULL
     AND resulting_claim_revision_number IS NULL
     AND resulting_preference_revision_id IS NOT NULL
     AND resulting_project_id IS NULL
     AND resulting_project_revision_id IS NULL)
    OR
    (lane = 'project_knowledge'
     AND resulting_claim_id IS NULL
     AND resulting_claim_revision_number IS NULL
     AND resulting_preference_revision_id IS NULL
     AND resulting_project_id IS NOT NULL
     AND resulting_project_revision_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS memory.preference_revision_observation (
  owner_user_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  stance memory.projection_observation_stance_v5 NOT NULL,
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, revision_id, observation_id, stance),
  FOREIGN KEY (owner_user_id, revision_id)
    REFERENCES memory.preference_revision(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS preference_revision_observation_owner_obs_idx
  ON memory.preference_revision_observation(owner_user_id, observation_id);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_revision_observation (
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  stance memory.projection_observation_stance_v5 NOT NULL,
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (
    owner_user_id, project_id, revision_id, observation_id, stance
  ),
  FOREIGN KEY (owner_user_id, project_id, revision_id)
    REFERENCES memory.project_knowledge_revision(
      owner_user_id, project_id, revision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (rationale IS NULL OR btrim(rationale) <> '')
);

CREATE INDEX IF NOT EXISTS project_revision_observation_owner_obs_idx
  ON memory.project_knowledge_revision_observation(
    owner_user_id, observation_id
  );

CREATE OR REPLACE FUNCTION memory.guard_projection_actor_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF current_user <> 'memory_v5_writer' THEN
    RAISE EXCEPTION 'projection staging writes require memory_v5_writer'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR NEW.owner_user_id <> actor THEN
    RAISE EXCEPTION 'projection staging owner does not match current actor'
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_projection_plan_complete_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  plan memory.projection_plan%ROWTYPE;
  actual_count integer;
  packet_ref_count integer;
BEGIN
  SELECT stored.* INTO plan
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.plan_id = NEW.plan_id;
  IF NOT FOUND THEN
    RETURN NEW;
  END IF;

  SELECT count(*) INTO actual_count
  FROM memory.projection_plan_item AS item
  WHERE item.owner_user_id = plan.owner_user_id
    AND item.plan_id = plan.plan_id;
  IF actual_count <> plan.projection_count THEN
    RAISE EXCEPTION 'projection plan item count mismatch'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(DISTINCT value->>'projection_ref') INTO packet_ref_count
  FROM jsonb_array_elements((plan.packet_text::jsonb)->'projections') AS rows(value);
  IF packet_ref_count <> plan.projection_count THEN
    RAISE EXCEPTION 'projection packet references are not unique'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_item AS item
    WHERE item.owner_user_id = plan.owner_user_id
      AND item.plan_id = plan.plan_id
      AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
          (plan.packet_text::jsonb)->'projections'
        ) AS rows(value)
        WHERE rows.value = item.projection
      )
  ) THEN
    RAISE EXCEPTION 'projection item is not bound to the exact packet'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_projection_item_complete_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  item memory.projection_plan_item%ROWTYPE;
  claim_payload memory.projection_claim_payload%ROWTYPE;
  preference_payload memory.projection_preference_payload%ROWTYPE;
  project_payload memory.projection_project_payload%ROWTYPE;
  payload_count integer;
  observation_count integer;
  relation_count integer;
  expected_payload jsonb;
  expected_scope jsonb;
  target_aggregate_id text;
  expected_class text;
  expected_surface memory.observation_surface_policy;
BEGIN
  SELECT stored.* INTO item
  FROM memory.projection_plan_item AS stored
  WHERE stored.owner_user_id = NEW.owner_user_id
    AND stored.plan_id = NEW.plan_id
    AND stored.projection_ref = NEW.projection_ref;
  IF NOT FOUND THEN
    RETURN NEW;
  END IF;

  SELECT
    (SELECT count(*) FROM memory.projection_claim_payload AS payload
     WHERE payload.owner_user_id = item.owner_user_id
       AND payload.plan_id = item.plan_id
       AND payload.projection_ref = item.projection_ref)
    +
    (SELECT count(*) FROM memory.projection_preference_payload AS payload
     WHERE payload.owner_user_id = item.owner_user_id
       AND payload.plan_id = item.plan_id
       AND payload.projection_ref = item.projection_ref)
    +
    (SELECT count(*) FROM memory.projection_project_payload AS payload
     WHERE payload.owner_user_id = item.owner_user_id
       AND payload.plan_id = item.plan_id
       AND payload.projection_ref = item.projection_ref)
  INTO payload_count;
  IF payload_count <> 1 THEN
    RAISE EXCEPTION 'projection item requires exactly one typed payload'
      USING ERRCODE = '23514';
  END IF;

  IF item.lane = 'claim' THEN
    SELECT payload.* INTO STRICT claim_payload
    FROM memory.projection_claim_payload AS payload
    WHERE payload.owner_user_id = item.owner_user_id
      AND payload.plan_id = item.plan_id
      AND payload.projection_ref = item.projection_ref;
    expected_scope := '{}'::jsonb;
    target_aggregate_id := claim_payload.target_claim_id::text;
    expected_class := claim_payload.claim_class;
    expected_surface := claim_payload.surface_policy;
    expected_payload := jsonb_build_object(
      'kind', 'claim',
      'claim_class', claim_payload.claim_class,
      'canonical_text', claim_payload.canonical_text,
      'surface_policy', claim_payload.surface_policy::text
    );
  ELSIF item.lane = 'preference' THEN
    SELECT payload.* INTO STRICT preference_payload
    FROM memory.projection_preference_payload AS payload
    WHERE payload.owner_user_id = item.owner_user_id
      AND payload.plan_id = item.plan_id
      AND payload.projection_ref = item.projection_ref;
    expected_scope := jsonb_build_object(
      'preference_class', preference_payload.preference_class,
      'domain', preference_payload.preference_domain,
      'preference_key', preference_payload.preference_key,
      'scope', preference_payload.scope
    );
    target_aggregate_id := preference_payload.target_preference_id::text;
    expected_class := preference_payload.preference_class || '_preference';
    expected_surface := preference_payload.surface_policy;
    expected_payload := jsonb_build_object(
      'kind', 'preference',
      'preference_class', preference_payload.preference_class,
      'domain', preference_payload.preference_domain,
      'preference_key', preference_payload.preference_key,
      'value', preference_payload.value,
      'preference_polarity', preference_payload.preference_polarity,
      'scope', preference_payload.scope,
      'stability', preference_payload.stability,
      'surface_policy', preference_payload.surface_policy::text
    );
    IF item.predicate <> 'preference.' || preference_payload.preference_class THEN
      RAISE EXCEPTION 'preference payload does not match predicate'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    SELECT payload.* INTO STRICT project_payload
    FROM memory.projection_project_payload AS payload
    WHERE payload.owner_user_id = item.owner_user_id
      AND payload.plan_id = item.plan_id
      AND payload.projection_ref = item.projection_ref;
    expected_scope := jsonb_build_object(
      'project_id', project_payload.project_id::text,
      'knowledge_kind', project_payload.knowledge_kind,
      'knowledge_key', project_payload.knowledge_key
    );
    target_aggregate_id := project_payload.target_knowledge_id::text;
    expected_class := 'project_knowledge';
    expected_surface := project_payload.surface_policy;
    expected_payload := jsonb_build_object(
      'kind', 'project_knowledge',
      'project_id', project_payload.project_id::text,
      'knowledge_kind', project_payload.knowledge_kind,
      'knowledge_key', project_payload.knowledge_key,
      'canonical_text', project_payload.canonical_text,
      'document_state', project_payload.document_state,
      'authority_level', project_payload.authority_level,
      'surface_policy', project_payload.surface_policy::text
    );
    IF project_payload.knowledge_kind <> (CASE item.predicate
         WHEN 'project.constraint' THEN 'constraint'
         WHEN 'project.current_state' THEN 'current_state'
         WHEN 'project.proposed_feature' THEN 'proposed_feature'
         WHEN 'project.requirement' THEN 'requirement'
         ELSE NULL
       END) THEN
      RAISE EXCEPTION 'project payload does not match predicate'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  IF item.lane_scope <> expected_scope
     OR item.projection->'payload' <> expected_payload
     OR NULLIF(item.projection#>>'{target,aggregate_id}', '')
          IS DISTINCT FROM target_aggregate_id THEN
    RAISE EXCEPTION 'typed payload does not match projection packet'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*) INTO observation_count
  FROM memory.projection_plan_observation AS link
  WHERE link.owner_user_id = item.owner_user_id
    AND link.plan_id = item.plan_id
    AND link.projection_ref = item.projection_ref;
  IF observation_count <> jsonb_array_length(item.projection->'observation_inputs')
     OR NOT EXISTS (
       SELECT 1 FROM memory.projection_plan_observation AS link
       WHERE link.owner_user_id = item.owner_user_id
         AND link.plan_id = item.plan_id
         AND link.projection_ref = item.projection_ref
         AND link.stance IN ('supports', 'context')
     ) THEN
    RAISE EXCEPTION 'projection observation set is incomplete'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_observation AS link
    WHERE link.owner_user_id = item.owner_user_id
      AND link.plan_id = item.plan_id
      AND link.projection_ref = item.projection_ref
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(
          item.projection->'observation_inputs'
        ) AS rows(value)
        WHERE rows.value = jsonb_build_object(
          'observation_id', link.observation_id::text,
          'observation_sha256', link.observation_sha256,
          'stance', link.stance::text
        )
      )
  ) THEN
    RAISE EXCEPTION 'projection observation link does not match packet'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id = link.owner_user_id
     AND observation.observation_id = link.observation_id
    JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id = observation.owner_user_id
     AND binding.observation_id = observation.observation_id
    WHERE link.owner_user_id = item.owner_user_id
      AND link.plan_id = item.plan_id
      AND link.projection_ref = item.projection_ref
      AND link.stance IN ('supports', 'context')
      AND (
        binding.subject_entity_id <> item.subject_entity_id
        OR observation.predicate <> item.predicate
        OR observation.polarity <> item.polarity
        OR observation.modality <> item.modality
        OR observation.projection_class::text <> expected_class
        OR observation.surface_policy <> expected_surface
        OR (
          item.object_kind = 'entity'
          AND binding.object_entity_id IS DISTINCT FROM item.object_entity_id
        )
        OR (
          item.object_kind = 'literal'
          AND memory.v5_digest_text(memory.v5_canonical_json_text(
            observation.object_literal
          )) <> item.object_literal_sha256
        )
      )
  ) THEN
    RAISE EXCEPTION 'projection observation identity or policy mismatch'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*) INTO relation_count
  FROM memory.projection_plan_relation AS relation
  WHERE relation.owner_user_id = item.owner_user_id
    AND relation.plan_id = item.plan_id
    AND relation.projection_ref = item.projection_ref;
  IF relation_count <> jsonb_array_length(item.projection->'relations') THEN
    RAISE EXCEPTION 'projection relation set is incomplete'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id = item.owner_user_id
      AND relation.plan_id = item.plan_id
      AND relation.projection_ref = item.projection_ref
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(item.projection->'relations') AS rows(value)
        WHERE rows.value = jsonb_build_object(
          'relation_type', relation.relation_type::text,
          'target_lane', relation.target_lane::text,
          'target_aggregate_id', COALESCE(
            relation.target_claim_id,
            relation.target_preference_id,
            relation.target_knowledge_id
          )::text,
          'reason_code', relation.reason_code
        )
      )
  ) THEN
    RAISE EXCEPTION 'projection relation does not match packet'
      USING ERRCODE = '23514';
  END IF;

  IF item.lane = 'claim'
     AND claim_payload.claim_class = 'correction'
     AND NOT EXISTS (
       SELECT 1 FROM memory.projection_plan_relation AS relation
       WHERE relation.owner_user_id = item.owner_user_id
         AND relation.plan_id = item.plan_id
         AND relation.projection_ref = item.projection_ref
         AND relation.relation_type IN ('corrects', 'supersedes')
     ) THEN
    RAISE EXCEPTION 'correction projection lacks reviewed target relation'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

DO $ownership$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'projection_plan',
    'projection_plan_item',
    'projection_claim_payload',
    'projection_preference_payload',
    'projection_project_payload',
    'projection_plan_observation',
    'projection_plan_relation',
    'projection_review',
    'projection_apply_event',
    'preference_revision_observation',
    'project_knowledge_revision_observation'
  ]
  LOOP
    EXECUTE format('ALTER TABLE memory.%I OWNER TO sage', table_name);
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I TO memory_v5_writer '
      'USING (owner_user_id = (SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()))',
      table_name
    );
    EXECUTE format('REVOKE ALL ON memory.%I FROM PUBLIC, brains_app', table_name);
    EXECUTE format('GRANT SELECT, INSERT ON memory.%I TO memory_v5_writer', table_name);

    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I ON memory.%I',
      table_name || '_actor_guard', table_name
    );
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE INSERT ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_projection_actor_v5()',
      table_name || '_actor_guard', table_name
    );
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I ON memory.%I',
      table_name || '_append_only_guard', table_name
    );
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only()',
      table_name || '_append_only_guard', table_name
    );
  END LOOP;
END
$ownership$;

DROP TRIGGER IF EXISTS projection_plan_complete_guard ON memory.projection_plan;
CREATE CONSTRAINT TRIGGER projection_plan_complete_guard
AFTER INSERT ON memory.projection_plan
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION memory.guard_projection_plan_complete_v5();

DROP TRIGGER IF EXISTS projection_item_plan_complete_guard
  ON memory.projection_plan_item;
CREATE CONSTRAINT TRIGGER projection_item_plan_complete_guard
AFTER INSERT ON memory.projection_plan_item
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION memory.guard_projection_plan_complete_v5();

DROP TRIGGER IF EXISTS projection_item_complete_guard
  ON memory.projection_plan_item;
CREATE CONSTRAINT TRIGGER projection_item_complete_guard
AFTER INSERT ON memory.projection_plan_item
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION memory.guard_projection_item_complete_v5();

DO $triggers$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'projection_claim_payload',
    'projection_preference_payload',
    'projection_project_payload',
    'projection_plan_observation',
    'projection_plan_relation'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I ON memory.%I',
      table_name || '_complete_guard', table_name
    );
    EXECUTE format(
      'CREATE CONSTRAINT TRIGGER %I AFTER INSERT ON memory.%I '
      'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW '
      'EXECUTE FUNCTION memory.guard_projection_item_complete_v5()',
      table_name || '_complete_guard', table_name
    );
  END LOOP;
END
$triggers$;

ALTER FUNCTION memory.v5_canonical_json_text(jsonb)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_projection_owner_manifest_sha256(uuid, text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_projection_semantic_key_sha256(
  uuid, memory.projection_lane_v5, uuid, text, text, uuid, text,
  memory.observation_polarity, memory.observation_modality, jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_actor_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_plan_complete_v5()
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.guard_projection_item_complete_v5()
  OWNER TO memory_v5_writer;

ALTER TYPE memory.projection_lane_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_target_action_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_review_state_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_observation_stance_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_temporal_materialization_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_relation_type_v5 OWNER TO memory_v5_writer;
ALTER TYPE memory.projection_review_decision_v5 OWNER TO memory_v5_writer;

GRANT USAGE ON TYPE
  memory.projection_lane_v5,
  memory.projection_target_action_v5,
  memory.projection_review_state_v5,
  memory.projection_observation_stance_v5,
  memory.projection_temporal_materialization_v5,
  memory.projection_relation_type_v5,
  memory.projection_review_decision_v5
TO memory_v5_writer;

GRANT SELECT ON
  memory.claim,
  memory.claim_revision,
  memory.user_preference,
  memory.preference_revision,
  memory.project_space,
  memory.project_knowledge_head,
  memory.project_knowledge_revision
TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.v5_canonical_json_text(jsonb)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_projection_owner_manifest_sha256(uuid, text)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_projection_semantic_key_sha256(
  uuid, memory.projection_lane_v5, uuid, text, text, uuid, text,
  memory.observation_polarity, memory.observation_modality, jsonb
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_projection_actor_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_projection_plan_complete_v5()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_projection_item_complete_v5()
  FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.v5_canonical_json_text(jsonb)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_projection_owner_manifest_sha256(uuid, text)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_projection_semantic_key_sha256(
  uuid, memory.projection_lane_v5, uuid, text, text, uuid, text,
  memory.observation_polarity, memory.observation_modality, jsonb
) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_actor_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_plan_complete_v5()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_projection_item_complete_v5()
  TO memory_v5_writer;

COMMIT;
