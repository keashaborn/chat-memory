BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection apply V5 migration must run as sage, current_user=%',
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
  IF to_regclass('memory.projection_plan') IS NULL
     OR to_regclass('memory.projection_review') IS NULL
     OR to_regclass('memory.projection_apply_event') IS NULL
     OR to_regclass('memory.preference_head_v5') IS NULL
     OR to_regclass('memory.project_knowledge_head_v5') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL THEN
    RAISE EXCEPTION 'V5 projection staging and durable targets are required';
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.claim_relation_v5 (
  owner_user_id uuid NOT NULL,
  relation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  apply_event_id uuid NOT NULL,
  from_claim_id uuid NOT NULL,
  to_claim_id uuid NOT NULL,
  relation_type memory.projection_relation_type_v5 NOT NULL,
  reason_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, relation_id),
  UNIQUE (
    owner_user_id, from_claim_id, to_claim_id, relation_type
  ),
  FOREIGN KEY (owner_user_id, apply_event_id)
    REFERENCES memory.projection_apply_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, from_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, to_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  CHECK (from_claim_id <> to_claim_id),
  CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,99}$')
);

CREATE TABLE IF NOT EXISTS memory.preference_relation_v5 (
  owner_user_id uuid NOT NULL,
  relation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  apply_event_id uuid NOT NULL,
  from_preference_id uuid NOT NULL,
  to_preference_id uuid NOT NULL,
  relation_type memory.projection_relation_type_v5 NOT NULL,
  reason_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, relation_id),
  UNIQUE (
    owner_user_id, from_preference_id, to_preference_id, relation_type
  ),
  FOREIGN KEY (owner_user_id, apply_event_id)
    REFERENCES memory.projection_apply_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, from_preference_id)
    REFERENCES memory.preference_head_v5(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, to_preference_id)
    REFERENCES memory.preference_head_v5(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  CHECK (from_preference_id <> to_preference_id),
  CHECK (relation_type <> 'corrects'),
  CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,99}$')
);

CREATE TABLE IF NOT EXISTS memory.project_knowledge_relation_v5 (
  owner_user_id uuid NOT NULL,
  relation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  apply_event_id uuid NOT NULL,
  project_id uuid NOT NULL,
  from_knowledge_id uuid NOT NULL,
  to_knowledge_id uuid NOT NULL,
  relation_type memory.projection_relation_type_v5 NOT NULL,
  reason_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, relation_id),
  UNIQUE (
    owner_user_id, project_id,
    from_knowledge_id, to_knowledge_id, relation_type
  ),
  FOREIGN KEY (owner_user_id, apply_event_id)
    REFERENCES memory.projection_apply_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, from_knowledge_id)
    REFERENCES memory.project_knowledge_head_v5(
      owner_user_id, project_id, knowledge_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, project_id, to_knowledge_id)
    REFERENCES memory.project_knowledge_head_v5(
      owner_user_id, project_id, knowledge_id
    ) ON DELETE RESTRICT,
  CHECK (from_knowledge_id <> to_knowledge_id),
  CHECK (relation_type <> 'corrects'),
  CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,99}$')
);

CREATE TABLE IF NOT EXISTS memory.projection_dispatch_v5 (
  owner_user_id uuid NOT NULL,
  dispatch_id uuid NOT NULL DEFAULT gen_random_uuid(),
  apply_event_id uuid NOT NULL,
  lane memory.projection_lane_v5 NOT NULL,
  operation text NOT NULL,
  resulting_claim_id uuid,
  resulting_claim_revision_number integer,
  resulting_preference_id uuid,
  resulting_preference_revision_id uuid,
  resulting_project_id uuid,
  resulting_knowledge_id uuid,
  resulting_project_revision_id uuid,
  payload jsonb NOT NULL,
  payload_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id, dispatch_id),
  UNIQUE (owner_user_id, apply_event_id),
  FOREIGN KEY (owner_user_id, apply_event_id)
    REFERENCES memory.projection_apply_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, resulting_claim_id, resulting_claim_revision_number
  ) REFERENCES memory.claim_revision(
    owner_user_id, claim_id, revision_number
  ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resulting_preference_id)
    REFERENCES memory.preference_head_v5(owner_user_id, preference_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resulting_preference_revision_id)
    REFERENCES memory.preference_revision_v5(owner_user_id, revision_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, resulting_project_id, resulting_knowledge_id
  ) REFERENCES memory.project_knowledge_head_v5(
    owner_user_id, project_id, knowledge_id
  ) ON DELETE RESTRICT,
  FOREIGN KEY (
    owner_user_id, resulting_project_id, resulting_project_revision_id
  ) REFERENCES memory.project_knowledge_revision_v5(
    owner_user_id, project_id, revision_id
  ) ON DELETE RESTRICT,
  CHECK (operation = 'upsert'),
  CHECK (jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 65536),
  CHECK (memory.v5_sha256_valid(payload_sha256)),
  CHECK (
    memory.v5_digest_text(memory.v5_canonical_json_text(payload))
      = payload_sha256
  ),
  CHECK (
    (lane = 'claim'
     AND resulting_claim_id IS NOT NULL
     AND resulting_claim_revision_number > 0
     AND resulting_preference_id IS NULL
     AND resulting_preference_revision_id IS NULL
     AND resulting_project_id IS NULL
     AND resulting_knowledge_id IS NULL
     AND resulting_project_revision_id IS NULL)
    OR
    (lane = 'preference'
     AND resulting_claim_id IS NULL
     AND resulting_claim_revision_number IS NULL
     AND resulting_preference_id IS NOT NULL
     AND resulting_preference_revision_id IS NOT NULL
     AND resulting_project_id IS NULL
     AND resulting_knowledge_id IS NULL
     AND resulting_project_revision_id IS NULL)
    OR
    (lane = 'project_knowledge'
     AND resulting_claim_id IS NULL
     AND resulting_claim_revision_number IS NULL
     AND resulting_preference_id IS NULL
     AND resulting_preference_revision_id IS NULL
     AND resulting_project_id IS NOT NULL
     AND resulting_knowledge_id IS NOT NULL
     AND resulting_project_revision_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS claim_relation_v5_owner_target_idx
  ON memory.claim_relation_v5(owner_user_id, to_claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS preference_relation_v5_owner_target_idx
  ON memory.preference_relation_v5(
    owner_user_id, to_preference_id, created_at DESC
  );
CREATE INDEX IF NOT EXISTS project_relation_v5_owner_target_idx
  ON memory.project_knowledge_relation_v5(
    owner_user_id, project_id, to_knowledge_id, created_at DESC
  );
CREATE INDEX IF NOT EXISTS projection_dispatch_v5_owner_lane_idx
  ON memory.projection_dispatch_v5(owner_user_id, lane, created_at);

CREATE OR REPLACE FUNCTION memory.v5_projection_review_manifest_sha256(
  p_owner_user_id uuid,
  p_plan_id uuid,
  p_projection_ref text,
  p_projection_sha256 text,
  p_semantic_key_sha256 text,
  p_review_number integer,
  p_decision memory.projection_review_decision_v5,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_reason text,
  p_reason_codes jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'manifest_version', 'memory_projection_review_v5',
      'owner_user_id', p_owner_user_id::text,
      'plan_id', p_plan_id::text,
      'projection_ref', p_projection_ref,
      'projection_sha256', p_projection_sha256,
      'semantic_key_sha256', p_semantic_key_sha256,
      'review_number', p_review_number,
      'decision', p_decision::text,
      'reviewer_type', p_reviewer_type,
      'reviewer_ref', p_reviewer_ref,
      'reason', p_reason,
      'reason_codes', p_reason_codes
    )
  ))
$function$;

CREATE OR REPLACE FUNCTION memory.v5_projection_apply_manifest_sha256(
  p_owner_user_id uuid,
  p_owner_manifest_sha256 text,
  p_plan_id uuid,
  p_projection_ref text,
  p_projection_sha256 text,
  p_semantic_key_sha256 text,
  p_lane memory.projection_lane_v5,
  p_target_action memory.projection_target_action_v5,
  p_current_revision_number integer,
  p_review_id uuid,
  p_review_manifest_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'manifest_version', 'memory_projection_apply_v5',
      'owner_user_id', p_owner_user_id::text,
      'owner_manifest_sha256', p_owner_manifest_sha256,
      'plan_id', p_plan_id::text,
      'projection_ref', p_projection_ref,
      'projection_sha256', p_projection_sha256,
      'semantic_key_sha256', p_semantic_key_sha256,
      'lane', p_lane::text,
      'target_action', p_target_action::text,
      'current_revision_number', p_current_revision_number,
      'review_id', p_review_id::text,
      'review_manifest_sha256', p_review_manifest_sha256
    )
  ))
$function$;

CREATE OR REPLACE FUNCTION memory.projection_apply_state_v5(
  p_plan_id uuid,
  p_projection_ref text,
  p_review_id uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  plan memory.projection_plan%ROWTYPE;
  item memory.projection_plan_item%ROWTYPE;
  review memory.projection_review%ROWTYPE;
  claim_payload memory.projection_claim_payload%ROWTYPE;
  preference_payload memory.projection_preference_payload%ROWTYPE;
  project_payload memory.projection_project_payload%ROWTYPE;
  claim_row memory.claim%ROWTYPE;
  preference_head memory.preference_head_v5%ROWTYPE;
  preference_revision memory.preference_revision_v5%ROWTYPE;
  project_head memory.project_knowledge_head_v5%ROWTYPE;
  project_revision memory.project_knowledge_revision_v5%ROWTYPE;
  current_revision_number integer;
  current_revision_id uuid;
  review_manifest text;
  apply_manifest text;
  expected_content_sha text;
  temporal_row memory.observation_temporal%ROWTYPE;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT stored.* INTO plan
  FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id = actor AND stored.plan_id = p_plan_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped projection plan not found'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT stored.* INTO item
  FROM memory.projection_plan_item AS stored
  WHERE stored.owner_user_id = actor
    AND stored.plan_id = p_plan_id
    AND stored.projection_ref = p_projection_ref;
  IF NOT FOUND OR item.target_action IN ('defer', 'reject')
     OR item.review_state IN ('deferred', 'rejected') THEN
    RAISE EXCEPTION 'owner-scoped applicable projection not found'
      USING ERRCODE = 'P0002';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id = link.owner_user_id
     AND observation.observation_id = link.observation_id
     AND observation.observation_sha256 = link.observation_sha256
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = observation.owner_user_id
     AND evidence.evidence_id = observation.evidence_id
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
      AND evidence.status <> 'active'
  ) OR NOT EXISTS (
    SELECT 1 FROM memory.projection_plan_observation AS link
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
  ) THEN
    RAISE EXCEPTION 'projection apply requires complete active evidence'
      USING ERRCODE = '23514';
  END IF;

  IF item.review_state = 'manual_review_required' THEN
    IF p_review_id IS NULL THEN
      RAISE EXCEPTION 'manual projection requires review_id'
        USING ERRCODE = '23514';
    END IF;
    SELECT stored.* INTO review
    FROM memory.projection_review AS stored
    WHERE stored.owner_user_id = actor
      AND stored.plan_id = p_plan_id
      AND stored.projection_ref = p_projection_ref
      AND stored.review_id = p_review_id;
    IF NOT FOUND OR review.decision <> 'authorized'
       OR review.review_id IS DISTINCT FROM (
         SELECT latest.review_id
         FROM memory.projection_review AS latest
         WHERE latest.owner_user_id = actor
           AND latest.plan_id = p_plan_id
           AND latest.projection_ref = p_projection_ref
         ORDER BY latest.review_number DESC
         LIMIT 1
       ) THEN
      RAISE EXCEPTION 'latest projection review is not supplied authorization'
        USING ERRCODE = '23514';
    END IF;
    review_manifest := review.authorization_manifest_sha256;
  ELSIF p_review_id IS NOT NULL THEN
    RAISE EXCEPTION 'auto-eligible projection cannot carry review_id'
      USING ERRCODE = '23514';
  END IF;

  IF item.target_action = 'relate' AND NOT EXISTS (
    SELECT 1 FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id = actor
      AND relation.plan_id = p_plan_id
      AND relation.projection_ref = p_projection_ref
  ) THEN
    RAISE EXCEPTION 'relate projection requires a reviewed relation'
      USING ERRCODE = '23514';
  END IF;

  IF item.lane = 'claim' THEN
    SELECT payload.* INTO STRICT claim_payload
    FROM memory.projection_claim_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    SELECT stored.* INTO claim_row
    FROM memory.claim AS stored
    WHERE stored.owner_user_id = actor
      AND (
        stored.claim_id = claim_payload.target_claim_id
        OR (item.target_action = 'create'
            AND stored.canonical_key = 'v5:' || item.semantic_key_sha256)
      );
    IF item.target_action = 'create' THEN
      IF FOUND THEN
        RAISE EXCEPTION 'claim semantic target already exists'
          USING ERRCODE = '23505';
      END IF;
      current_revision_number := 0;
    ELSE
      IF NOT FOUND OR claim_row.claim_id <> claim_payload.target_claim_id
         OR claim_row.status IN ('superseded', 'retracted', 'quarantined')
         OR claim_row.canonical_key <> 'v5:' || item.semantic_key_sha256 THEN
        RAISE EXCEPTION 'claim target identity or state mismatch'
          USING ERRCODE = '23514';
      END IF;
      SELECT COALESCE(max(revision_number), 0)
      INTO current_revision_number
      FROM memory.claim_revision AS revision
      WHERE revision.owner_user_id = actor
        AND revision.claim_id = claim_row.claim_id;
      IF current_revision_number <> item.expected_revision_number THEN
        RAISE EXCEPTION 'claim optimistic revision mismatch'
          USING ERRCODE = '40001';
      END IF;
      IF item.target_action = 'reinforce'
         AND (claim_row.canonical_text <> claim_payload.canonical_text
              OR claim_row.retrieval_policy->>'surface_policy'
                   IS DISTINCT FROM claim_payload.surface_policy::text) THEN
        RAISE EXCEPTION 'claim reinforcement content mismatch'
          USING ERRCODE = '23514';
      END IF;
    END IF;
  ELSIF item.lane = 'preference' THEN
    SELECT payload.* INTO STRICT preference_payload
    FROM memory.projection_preference_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    SELECT stored.* INTO preference_head
    FROM memory.preference_head_v5 AS stored
    WHERE stored.owner_user_id = actor
      AND (
        stored.preference_id = preference_payload.target_preference_id
        OR (item.target_action = 'create'
            AND stored.semantic_key_sha256 = item.semantic_key_sha256)
      );
    IF item.target_action = 'create' THEN
      IF FOUND THEN
        RAISE EXCEPTION 'preference semantic target already exists'
          USING ERRCODE = '23505';
      END IF;
      current_revision_number := 0;
    ELSE
      IF NOT FOUND
         OR preference_head.preference_id
              <> preference_payload.target_preference_id
         OR preference_head.status <> 'active'
         OR preference_head.semantic_key_sha256 <> item.semantic_key_sha256 THEN
        RAISE EXCEPTION 'preference target identity or state mismatch'
          USING ERRCODE = '23514';
      END IF;
      current_revision_number := preference_head.revision_number;
      current_revision_id := preference_head.current_revision_id;
      IF current_revision_number <> item.expected_revision_number THEN
        RAISE EXCEPTION 'preference optimistic revision mismatch'
          USING ERRCODE = '40001';
      END IF;
      SELECT stored.* INTO STRICT preference_revision
      FROM memory.preference_revision_v5 AS stored
      WHERE stored.owner_user_id = actor
        AND stored.preference_id = preference_head.preference_id
        AND stored.revision_id = preference_head.current_revision_id;
      expected_content_sha := memory.v5_digest_text(
        memory.v5_canonical_json_text(jsonb_build_object(
          'value', preference_payload.value,
          'preference_polarity', preference_payload.preference_polarity,
          'stability', preference_payload.stability,
          'surface_policy', preference_payload.surface_policy::text
        ))
      );
      IF item.target_action = 'reinforce'
         AND preference_revision.content_sha256 <> expected_content_sha THEN
        RAISE EXCEPTION 'preference reinforcement content mismatch'
          USING ERRCODE = '23514';
      END IF;
    END IF;
  ELSE
    SELECT payload.* INTO STRICT project_payload
    FROM memory.projection_project_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    PERFORM 1 FROM memory.project_space AS project
    WHERE project.owner_user_id = actor
      AND project.project_id = project_payload.project_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'owner-scoped project is not registered'
        USING ERRCODE = '23514';
    END IF;
    IF project_payload.component_key IS NOT NULL THEN
      IF project_payload.binding_source <> 'trusted_component_registry'
         OR to_regclass('memory.project_component_v5') IS NULL
         OR NOT EXISTS (
           SELECT 1 FROM memory.project_component_v5 AS component
           WHERE component.owner_user_id = actor
             AND component.project_id = project_payload.project_id
             AND component.component_key = project_payload.component_key
         ) THEN
        RAISE EXCEPTION 'owner-scoped project component is not registered'
          USING ERRCODE = '23514';
      END IF;
    ELSIF project_payload.binding_source NOT IN (
      'explicit_source_text', 'trusted_thread_binding', 'legacy_root_scope'
    ) THEN
      RAISE EXCEPTION 'root project binding source is invalid'
        USING ERRCODE = '23514';
    END IF;
    SELECT stored.* INTO project_head
    FROM memory.project_knowledge_head_v5 AS stored
    WHERE stored.owner_user_id = actor
      AND stored.project_id = project_payload.project_id
      AND stored.component_key IS NOT DISTINCT FROM project_payload.component_key
      AND (
        stored.knowledge_id = project_payload.target_knowledge_id
        OR (item.target_action = 'create'
            AND stored.semantic_key_sha256 = item.semantic_key_sha256)
      );
    IF item.target_action = 'create' THEN
      IF FOUND THEN
        RAISE EXCEPTION 'project semantic target already exists'
          USING ERRCODE = '23505';
      END IF;
      current_revision_number := 0;
    ELSE
      IF NOT FOUND OR project_head.knowledge_id
            <> project_payload.target_knowledge_id
         OR project_head.status <> 'active'
         OR project_head.component_key IS DISTINCT FROM project_payload.component_key
         OR project_head.binding_source IS DISTINCT FROM project_payload.binding_source
         OR project_head.semantic_key_sha256 <> item.semantic_key_sha256 THEN
        RAISE EXCEPTION 'project target identity or state mismatch'
          USING ERRCODE = '23514';
      END IF;
      current_revision_number := project_head.revision_number;
      current_revision_id := project_head.current_revision_id;
      IF current_revision_number <> item.expected_revision_number THEN
        RAISE EXCEPTION 'project optimistic revision mismatch'
          USING ERRCODE = '40001';
      END IF;
      SELECT stored.* INTO STRICT project_revision
      FROM memory.project_knowledge_revision_v5 AS stored
      WHERE stored.owner_user_id = actor
        AND stored.project_id = project_head.project_id
        AND stored.knowledge_id = project_head.knowledge_id
        AND stored.revision_id = project_head.current_revision_id;
      expected_content_sha := memory.v5_digest_text(
        memory.v5_canonical_json_text(jsonb_build_object(
          'canonical_text', project_payload.canonical_text,
          'document_state', project_payload.document_state,
          'authority_level', project_payload.authority_level,
          'surface_policy', project_payload.surface_policy::text
        ))
      );
      IF item.target_action = 'reinforce'
         AND project_revision.content_sha256 <> expected_content_sha THEN
        RAISE EXCEPTION 'project reinforcement content mismatch'
          USING ERRCODE = '23514';
      END IF;
    END IF;
    IF item.temporal_materialization = 'state_validity_only' THEN
      SELECT stored.* INTO temporal_row
      FROM memory.observation_temporal AS stored
      WHERE stored.owner_user_id = actor
        AND stored.observation_id = item.temporal_source_observation_id;
      IF NOT FOUND OR temporal_row.semantic <> 'state_validity'
         OR temporal_row.basis NOT IN ('instant', 'calendar') THEN
        RAISE EXCEPTION 'project temporal materialization is not exact'
          USING ERRCODE = '23514';
      END IF;
    END IF;
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.projection_plan_relation AS relation
    LEFT JOIN memory.claim AS target_claim
      ON relation.target_lane = 'claim'
     AND target_claim.owner_user_id = relation.owner_user_id
     AND target_claim.claim_id = relation.target_claim_id
    LEFT JOIN memory.preference_head_v5 AS target_preference
      ON relation.target_lane = 'preference'
     AND target_preference.owner_user_id = relation.owner_user_id
     AND target_preference.preference_id = relation.target_preference_id
    LEFT JOIN memory.project_knowledge_head_v5 AS target_project
      ON relation.target_lane = 'project_knowledge'
     AND target_project.owner_user_id = relation.owner_user_id
     AND target_project.project_id = relation.target_project_id
     AND target_project.knowledge_id = relation.target_knowledge_id
    WHERE relation.owner_user_id = actor
      AND relation.plan_id = p_plan_id
      AND relation.projection_ref = p_projection_ref
      AND (
        (relation.target_lane = 'claim'
         AND (target_claim.claim_id IS NULL
              OR target_claim.status IN ('superseded','retracted','quarantined')))
        OR (relation.target_lane = 'preference'
            AND target_preference.status IS DISTINCT FROM 'active')
        OR (relation.target_lane = 'project_knowledge'
            AND (target_project.status IS DISTINCT FROM 'active'
                 OR target_project.project_id IS DISTINCT FROM
                      project_payload.project_id))
      )
  ) THEN
    RAISE EXCEPTION 'projection relation target is missing or inactive'
      USING ERRCODE = '23514';
  END IF;

  apply_manifest := memory.v5_projection_apply_manifest_sha256(
    actor, plan.owner_manifest_sha256, p_plan_id, p_projection_ref,
    item.projection_sha256, item.semantic_key_sha256,
    item.lane, item.target_action, current_revision_number,
    p_review_id, review_manifest
  );
  RETURN jsonb_build_object(
    'owner_user_id', actor,
    'plan_id', p_plan_id,
    'projection_ref', p_projection_ref,
    'lane', item.lane,
    'target_action', item.target_action,
    'review_state', item.review_state,
    'current_revision_number', current_revision_number,
    'current_revision_id', current_revision_id,
    'review_id', p_review_id,
    'review_manifest_sha256', review_manifest,
    'apply_manifest_sha256', apply_manifest
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_review_v5(
  p_plan_id uuid,
  p_projection_ref text,
  p_decision memory.projection_review_decision_v5,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_reason text,
  p_reason_codes jsonb
)
RETURNS TABLE(
  plan_id uuid,
  projection_ref text,
  review_number integer,
  projection_sha256 text,
  semantic_key_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  item memory.projection_plan_item%ROWTYPE;
  next_review integer;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_reviewer_type NOT IN ('user', 'system', 'job', 'admin')
     OR length(COALESCE(p_reviewer_ref, '')) > 500
     OR btrim(COALESCE(p_reason, '')) = '' OR length(p_reason) > 2000
     OR NOT memory.v5_reason_codes_valid(p_reason_codes, 20) THEN
    RAISE EXCEPTION 'projection review inputs are invalid'
      USING ERRCODE = '22023';
  END IF;
  SELECT stored.* INTO item
  FROM memory.projection_plan_item AS stored
  WHERE stored.owner_user_id = actor
    AND stored.plan_id = p_plan_id
    AND stored.projection_ref = p_projection_ref;
  IF NOT FOUND OR item.review_state <> 'manual_review_required' THEN
    RAISE EXCEPTION 'owner-scoped manual-review projection not found'
      USING ERRCODE = 'P0002';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.projection_plan_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id = link.owner_user_id
     AND observation.observation_id = link.observation_id
     AND observation.observation_sha256 = link.observation_sha256
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = observation.owner_user_id
     AND evidence.evidence_id = observation.evidence_id
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
      AND evidence.status <> 'active'
  ) THEN
    RAISE EXCEPTION 'projection review requires active evidence'
      USING ERRCODE = '23514';
  END IF;
  SELECT COALESCE(max(stored.review_number), 0) + 1
  INTO next_review
  FROM memory.projection_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.plan_id = p_plan_id
    AND stored.projection_ref = p_projection_ref;
  manifest := memory.v5_projection_review_manifest_sha256(
    actor, p_plan_id, p_projection_ref, item.projection_sha256,
    item.semantic_key_sha256, next_review, p_decision,
    p_reviewer_type, p_reviewer_ref, btrim(p_reason), p_reason_codes
  );
  RETURN QUERY SELECT p_plan_id, p_projection_ref, next_review,
    item.projection_sha256, item.semantic_key_sha256, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_projection_v5(
  p_plan_id uuid,
  p_projection_ref text,
  p_decision memory.projection_review_decision_v5,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_reason text,
  p_reason_codes jsonb,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(review_id uuid, outcome text, rows_written integer, result jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  existing memory.projection_review%ROWTYPE;
  preflight record;
  new_review_id uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'review authorization manifest is invalid'
      USING ERRCODE = '22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|projection-review|' || p_plan_id::text || '|' ||
    p_projection_ref, 0
  ));
  SELECT stored.* INTO existing
  FROM memory.projection_review AS stored
  WHERE stored.owner_user_id = actor
    AND stored.plan_id = p_plan_id
    AND stored.projection_ref = p_projection_ref
    AND stored.authorization_manifest_sha256
          = p_authorization_manifest_sha256;
  IF FOUND THEN
    IF existing.decision <> p_decision
       OR existing.reviewer_type <> p_reviewer_type
       OR existing.reviewer_ref IS DISTINCT FROM p_reviewer_ref
       OR existing.reason <> btrim(p_reason)
       OR existing.reason_codes <> p_reason_codes THEN
      RAISE EXCEPTION 'review manifest replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    result_value := jsonb_build_object(
      'review_id', existing.review_id,
      'review_number', existing.review_number,
      'decision', existing.decision
    );
    RETURN QUERY SELECT existing.review_id, 'replayed', 0, result_value;
    RETURN;
  END IF;
  SELECT * INTO preflight
  FROM memory.preflight_projection_review_v5(
    p_plan_id, p_projection_ref, p_decision, p_reviewer_type,
    p_reviewer_ref, p_reason, p_reason_codes
  );
  IF preflight.authorization_manifest_sha256
       <> p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'review authorization manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  INSERT INTO memory.projection_review(
    owner_user_id, plan_id, projection_ref, review_id, review_number,
    parent_review_state, decision, expected_projection_sha256,
    expected_semantic_key_sha256, reviewer_type, reviewer_ref,
    reason, reason_codes, authorization_manifest_sha256
  ) VALUES (
    actor, p_plan_id, p_projection_ref, new_review_id,
    preflight.review_number, 'manual_review_required', p_decision,
    preflight.projection_sha256, preflight.semantic_key_sha256,
    p_reviewer_type, p_reviewer_ref, btrim(p_reason), p_reason_codes,
    p_authorization_manifest_sha256
  );
  result_value := jsonb_build_object(
    'review_id', new_review_id,
    'review_number', preflight.review_number,
    'decision', p_decision
  );
  RETURN QUERY SELECT new_review_id, 'applied', 1, result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_apply_v5(
  p_plan_id uuid,
  p_projection_ref text,
  p_review_id uuid DEFAULT NULL
)
RETURNS TABLE(
  plan_id uuid,
  projection_ref text,
  lane memory.projection_lane_v5,
  target_action memory.projection_target_action_v5,
  review_state memory.projection_review_state_v5,
  current_revision_number integer,
  review_id uuid,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  state jsonb;
BEGIN
  state := memory.projection_apply_state_v5(
    p_plan_id, p_projection_ref, p_review_id
  );
  RETURN QUERY SELECT
    p_plan_id,
    p_projection_ref,
    (state->>'lane')::memory.projection_lane_v5,
    (state->>'target_action')::memory.projection_target_action_v5,
    (state->>'review_state')::memory.projection_review_state_v5,
    (state->>'current_revision_number')::integer,
    NULLIF(state->>'review_id', '')::uuid,
    state->>'apply_manifest_sha256';
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_projection_v5(
  p_request_id uuid,
  p_plan_id uuid,
  p_projection_ref text,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  apply_event_id uuid,
  outcome text,
  lane memory.projection_lane_v5,
  aggregate_id uuid,
  revision_id uuid,
  revision_number integer,
  rows_written integer,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  item memory.projection_plan_item%ROWTYPE;
  claim_payload memory.projection_claim_payload%ROWTYPE;
  preference_payload memory.projection_preference_payload%ROWTYPE;
  project_payload memory.projection_project_payload%ROWTYPE;
  claim_row memory.claim%ROWTYPE;
  preference_head memory.preference_head_v5%ROWTYPE;
  project_head memory.project_knowledge_head_v5%ROWTYPE;
  temporal_row memory.observation_temporal%ROWTYPE;
  existing memory.projection_apply_event%ROWTYPE;
  state jsonb;
  lane_value memory.projection_lane_v5;
  action_value memory.projection_target_action_v5;
  review_state_value memory.projection_review_state_v5;
  event_id_value uuid := gen_random_uuid();
  aggregate_id_value uuid;
  revision_id_value uuid;
  revision_number_value integer;
  project_id_value uuid;
  component_key_value text;
  binding_source_value text;
  object_literal_value jsonb;
  sensitivity_value memory.sensitivity_level;
  content_value jsonb;
  content_sha text;
  result_value jsonb;
  dispatch_payload jsonb;
  written integer := 0;
  step_count integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL OR p_plan_id IS NULL
     OR btrim(COALESCE(p_projection_ref, '')) = ''
     OR NOT memory.v5_sha256_valid(p_apply_manifest_sha256) THEN
    RAISE EXCEPTION 'projection apply identifiers or manifest are invalid'
      USING ERRCODE = '22023';
  END IF;

  SELECT stored.* INTO existing
  FROM memory.projection_apply_event AS stored
  WHERE stored.owner_user_id = actor
    AND stored.request_id = p_request_id;
  IF FOUND THEN
    IF existing.plan_id <> p_plan_id
       OR existing.projection_ref <> p_projection_ref
       OR existing.review_id IS DISTINCT FROM p_review_id
       OR existing.apply_manifest_sha256 <> p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    SELECT COALESCE(
      existing.resulting_claim_id,
      preference.preference_id,
      project_revision.knowledge_id
    ), COALESCE(
      claim_revision.revision_id,
      existing.resulting_preference_revision_id,
      existing.resulting_project_revision_id
    ), COALESCE(
      existing.resulting_claim_revision_number,
      preference_revision.revision_number,
      project_revision.revision_number
    ), existing.resulting_project_id,
      replay_project_head.component_key, replay_project_head.binding_source
    INTO aggregate_id_value, revision_id_value,
      revision_number_value, project_id_value,
      component_key_value, binding_source_value
    FROM (SELECT 1) AS singleton
    LEFT JOIN memory.claim_revision AS claim_revision
      ON claim_revision.owner_user_id = existing.owner_user_id
     AND claim_revision.claim_id = existing.resulting_claim_id
     AND claim_revision.revision_number
           = existing.resulting_claim_revision_number
    LEFT JOIN memory.preference_revision_v5 AS preference_revision
      ON preference_revision.owner_user_id = existing.owner_user_id
     AND preference_revision.revision_id
           = existing.resulting_preference_revision_id
    LEFT JOIN memory.preference_head_v5 AS preference
      ON preference.owner_user_id = preference_revision.owner_user_id
     AND preference.preference_id = preference_revision.preference_id
    LEFT JOIN memory.project_knowledge_revision_v5 AS project_revision
      ON project_revision.owner_user_id = existing.owner_user_id
     AND project_revision.project_id = existing.resulting_project_id
     AND project_revision.revision_id
           = existing.resulting_project_revision_id
    LEFT JOIN memory.project_knowledge_head_v5 AS replay_project_head
      ON replay_project_head.owner_user_id = project_revision.owner_user_id
     AND replay_project_head.project_id = project_revision.project_id
     AND replay_project_head.knowledge_id = project_revision.knowledge_id;
    result_value := jsonb_build_object(
      'apply_event_id', existing.event_id,
      'lane', existing.lane,
      'aggregate_id', aggregate_id_value,
      'revision_id', revision_id_value,
      'revision_number', revision_number_value,
      'project_id', project_id_value,
      'component_key', component_key_value,
      'binding_source', binding_source_value
    );
    RETURN QUERY SELECT existing.event_id, 'replayed', existing.lane,
      aggregate_id_value, revision_id_value, revision_number_value,
      0, result_value;
    RETURN;
  END IF;
  SELECT stored.* INTO existing
  FROM memory.projection_apply_event AS stored
  WHERE stored.owner_user_id = actor
    AND stored.apply_manifest_sha256 = p_apply_manifest_sha256;
  IF FOUND THEN
    SELECT * INTO result_value
    FROM jsonb_build_object(
      'apply_event_id', existing.event_id,
      'lane', existing.lane,
      'claim_id', existing.resulting_claim_id,
      'claim_revision_number', existing.resulting_claim_revision_number,
      'preference_revision_id', existing.resulting_preference_revision_id,
      'project_id', existing.resulting_project_id,
      'project_revision_id', existing.resulting_project_revision_id
    );
    IF existing.plan_id <> p_plan_id
       OR existing.projection_ref <> p_projection_ref
       OR existing.review_id IS DISTINCT FROM p_review_id THEN
      RAISE EXCEPTION 'apply manifest replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    IF existing.lane = 'claim' THEN
      aggregate_id_value := existing.resulting_claim_id;
      revision_number_value := existing.resulting_claim_revision_number;
      SELECT stored.revision_id INTO revision_id_value
      FROM memory.claim_revision AS stored
      WHERE stored.owner_user_id = actor
        AND stored.claim_id = aggregate_id_value
        AND stored.revision_number = revision_number_value;
    ELSIF existing.lane = 'preference' THEN
      revision_id_value := existing.resulting_preference_revision_id;
      SELECT stored.preference_id, stored.revision_number
      INTO aggregate_id_value, revision_number_value
      FROM memory.preference_revision_v5 AS stored
      WHERE stored.owner_user_id = actor
        AND stored.revision_id = revision_id_value;
    ELSE
      project_id_value := existing.resulting_project_id;
      revision_id_value := existing.resulting_project_revision_id;
      SELECT stored.knowledge_id, stored.revision_number
      INTO aggregate_id_value, revision_number_value
      FROM memory.project_knowledge_revision_v5 AS stored
      WHERE stored.owner_user_id = actor
        AND stored.project_id = project_id_value
        AND stored.revision_id = revision_id_value;
      SELECT stored.component_key, stored.binding_source
      INTO component_key_value, binding_source_value
      FROM memory.project_knowledge_head_v5 AS stored
      WHERE stored.owner_user_id = actor
        AND stored.project_id = project_id_value
        AND stored.knowledge_id = aggregate_id_value;
    END IF;
    result_value := result_value || jsonb_build_object(
      'aggregate_id', aggregate_id_value,
      'revision_id', revision_id_value,
      'revision_number', revision_number_value,
      'component_key', component_key_value,
      'binding_source', binding_source_value
    );
    RETURN QUERY SELECT existing.event_id, 'replayed', existing.lane,
      aggregate_id_value, revision_id_value, revision_number_value,
      0, result_value;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|projection-item|' || p_plan_id::text || '|' ||
    p_projection_ref, 0
  ));
  SELECT stored.* INTO item
  FROM memory.projection_plan_item AS stored
  WHERE stored.owner_user_id = actor
    AND stored.plan_id = p_plan_id
    AND stored.projection_ref = p_projection_ref;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped projection item not found'
      USING ERRCODE = 'P0002';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|projection-semantic|' || item.lane::text || '|' ||
    item.semantic_key_sha256, 0
  ));
  PERFORM 1
  FROM memory.projection_plan_observation AS link
  JOIN memory.observation AS observation
    ON observation.owner_user_id = link.owner_user_id
   AND observation.observation_id = link.observation_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = observation.owner_user_id
   AND evidence.evidence_id = observation.evidence_id
  WHERE link.owner_user_id = actor
    AND link.plan_id = p_plan_id
    AND link.projection_ref = p_projection_ref
  FOR UPDATE OF evidence;

  IF item.lane = 'claim' THEN
    SELECT payload.* INTO STRICT claim_payload
    FROM memory.projection_claim_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    IF claim_payload.target_claim_id IS NOT NULL THEN
      PERFORM 1 FROM memory.claim AS target
      WHERE target.owner_user_id = actor
        AND target.claim_id = claim_payload.target_claim_id
      FOR UPDATE;
    END IF;
  ELSIF item.lane = 'preference' THEN
    SELECT payload.* INTO STRICT preference_payload
    FROM memory.projection_preference_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    IF preference_payload.target_preference_id IS NOT NULL THEN
      SELECT target.* INTO STRICT preference_head
      FROM memory.preference_head_v5 AS target
      WHERE target.owner_user_id = actor
        AND target.preference_id = preference_payload.target_preference_id
      FOR UPDATE;
    END IF;
  ELSE
    SELECT payload.* INTO STRICT project_payload
    FROM memory.projection_project_payload AS payload
    WHERE payload.owner_user_id = actor
      AND payload.plan_id = p_plan_id
      AND payload.projection_ref = p_projection_ref;
    component_key_value := project_payload.component_key;
    binding_source_value := project_payload.binding_source;
    IF project_payload.target_knowledge_id IS NOT NULL THEN
      SELECT target.* INTO STRICT project_head
      FROM memory.project_knowledge_head_v5 AS target
      WHERE target.owner_user_id = actor
        AND target.project_id = project_payload.project_id
        AND target.component_key IS NOT DISTINCT FROM project_payload.component_key
        AND target.knowledge_id = project_payload.target_knowledge_id
      FOR UPDATE;
    END IF;
  END IF;

  state := memory.projection_apply_state_v5(
    p_plan_id, p_projection_ref, p_review_id
  );
  IF state->>'apply_manifest_sha256' <> p_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'projection apply manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  lane_value := (state->>'lane')::memory.projection_lane_v5;
  action_value :=
    (state->>'target_action')::memory.projection_target_action_v5;
  review_state_value :=
    (state->>'review_state')::memory.projection_review_state_v5;

  IF lane_value = 'claim' THEN
    SELECT observation.object_literal
    INTO object_literal_value
    FROM memory.projection_plan_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id = link.owner_user_id
     AND observation.observation_id = link.observation_id
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
      AND link.stance IN ('supports', 'context')
    ORDER BY CASE link.stance WHEN 'supports' THEN 0 ELSE 1 END,
      link.observation_id
    LIMIT 1;
    SELECT observation.sensitivity
    INTO sensitivity_value
    FROM memory.projection_plan_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id = link.owner_user_id
     AND observation.observation_id = link.observation_id
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
    ORDER BY CASE observation.sensitivity
      WHEN 'restricted' THEN 4 WHEN 'high' THEN 3
      WHEN 'medium' THEN 2 ELSE 1 END DESC
    LIMIT 1;
    IF action_value = 'create' THEN
      INSERT INTO memory.claim(
        owner_user_id, subject_entity_id, predicate,
        object_entity_id, object_literal, canonical_text, canonical_key,
        status, sensitivity, retrieval_policy, metadata
      ) VALUES (
        actor, item.subject_entity_id, item.predicate,
        item.object_entity_id,
        CASE WHEN item.object_kind = 'literal'
          THEN object_literal_value ELSE NULL END,
        claim_payload.canonical_text, 'v5:' || item.semantic_key_sha256,
        'candidate', sensitivity_value,
        jsonb_build_object(
          'surface_policy', claim_payload.surface_policy::text,
          'projection_class', claim_payload.claim_class
        ),
        jsonb_build_object(
          'memory_contract', 'memory_projection_v5',
          'semantic_key_sha256', item.semantic_key_sha256,
          'projection_sha256', item.projection_sha256
        )
      ) RETURNING * INTO claim_row;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
      aggregate_id_value := claim_row.claim_id;
      revision_number_value := 1;
      INSERT INTO memory.claim_revision AS created_revision(
        owner_user_id, claim_id, revision_number, snapshot,
        reason, actor_type, actor_ref
      ) VALUES (
        actor, aggregate_id_value, revision_number_value,
        to_jsonb(claim_row), 'projection_v5_create', 'system',
        p_apply_manifest_sha256
      ) RETURNING created_revision.revision_id INTO revision_id_value;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
    ELSE
      aggregate_id_value := claim_payload.target_claim_id;
      revision_number_value :=
        (state->>'current_revision_number')::integer;
      IF action_value = 'revise' THEN
        UPDATE memory.claim AS target SET
          canonical_text = claim_payload.canonical_text,
          retrieval_policy = target.retrieval_policy || jsonb_build_object(
            'surface_policy', claim_payload.surface_policy::text,
            'projection_class', claim_payload.claim_class
          ),
          metadata = target.metadata || jsonb_build_object(
            'last_projection_sha256', item.projection_sha256
          )
        WHERE target.owner_user_id = actor
          AND target.claim_id = aggregate_id_value
        RETURNING * INTO claim_row;
        GET DIAGNOSTICS step_count = ROW_COUNT;
        written := written + step_count;
        revision_number_value := revision_number_value + 1;
        INSERT INTO memory.claim_revision AS created_revision(
          owner_user_id, claim_id, revision_number, snapshot,
          reason, actor_type, actor_ref
        ) VALUES (
          actor, aggregate_id_value, revision_number_value,
          to_jsonb(claim_row), 'projection_v5_revise', 'system',
          p_apply_manifest_sha256
        ) RETURNING created_revision.revision_id INTO revision_id_value;
        GET DIAGNOSTICS step_count = ROW_COUNT;
        written := written + step_count;
      ELSE
        SELECT stored.revision_id INTO revision_id_value
        FROM memory.claim_revision AS stored
        WHERE stored.owner_user_id = actor
          AND stored.claim_id = aggregate_id_value
          AND stored.revision_number = revision_number_value;
      END IF;
    END IF;
    INSERT INTO memory.claim_observation(
      owner_user_id, claim_id, observation_id, stance, relevance, rationale
    )
    SELECT actor, aggregate_id_value, link.observation_id,
      CASE link.stance
        WHEN 'supports' THEN 'supports'::memory.evidence_stance
        WHEN 'opposes' THEN 'opposes'::memory.evidence_stance
        WHEN 'context' THEN 'context'::memory.evidence_stance
        ELSE 'opposes'::memory.evidence_stance
      END,
      1.000, 'memory_projection_v5'
    FROM memory.projection_plan_observation AS link
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS step_count = ROW_COUNT;
    written := written + step_count;
  ELSIF lane_value = 'preference' THEN
    content_value := jsonb_build_object(
      'value', preference_payload.value,
      'preference_polarity', preference_payload.preference_polarity,
      'stability', preference_payload.stability,
      'surface_policy', preference_payload.surface_policy::text
    );
    content_sha := memory.v5_digest_text(
      memory.v5_canonical_json_text(content_value)
    );
    IF action_value = 'create' THEN
      INSERT INTO memory.preference_head_v5(
        owner_user_id, semantic_key_sha256, preference_class,
        preference_domain, preference_key, scope
      ) VALUES (
        actor, item.semantic_key_sha256,
        preference_payload.preference_class,
        preference_payload.preference_domain,
        preference_payload.preference_key, preference_payload.scope
      ) RETURNING * INTO preference_head;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
      aggregate_id_value := preference_head.preference_id;
      revision_number_value := 1;
      INSERT INTO memory.preference_revision_v5 AS created_revision(
        owner_user_id, preference_id, revision_number, value,
        preference_polarity, stability, surface_policy,
        content_sha256, prior_revision_id, metadata
      ) VALUES (
        actor, aggregate_id_value, revision_number_value,
        preference_payload.value, preference_payload.preference_polarity,
        preference_payload.stability, preference_payload.surface_policy,
        content_sha, NULL,
        jsonb_build_object('projection_sha256', item.projection_sha256)
      ) RETURNING created_revision.revision_id INTO revision_id_value;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
      UPDATE memory.preference_head_v5 SET
        current_revision_id = revision_id_value,
        revision_number = revision_number_value,
        updated_at = clock_timestamp()
      WHERE owner_user_id = actor
        AND preference_id = aggregate_id_value;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
    ELSE
      aggregate_id_value := preference_payload.target_preference_id;
      revision_number_value := preference_head.revision_number;
      revision_id_value := preference_head.current_revision_id;
      IF action_value = 'revise' THEN
        revision_number_value := revision_number_value + 1;
        INSERT INTO memory.preference_revision_v5 AS created_revision(
          owner_user_id, preference_id, revision_number, value,
          preference_polarity, stability, surface_policy,
          content_sha256, prior_revision_id, metadata
        ) VALUES (
          actor, aggregate_id_value, revision_number_value,
          preference_payload.value, preference_payload.preference_polarity,
          preference_payload.stability, preference_payload.surface_policy,
          content_sha, preference_head.current_revision_id,
          jsonb_build_object('projection_sha256', item.projection_sha256)
        ) RETURNING created_revision.revision_id INTO revision_id_value;
        GET DIAGNOSTICS step_count = ROW_COUNT;
        written := written + step_count;
        UPDATE memory.preference_head_v5 SET
          current_revision_id = revision_id_value,
          revision_number = revision_number_value,
          updated_at = clock_timestamp()
        WHERE owner_user_id = actor
          AND preference_id = aggregate_id_value;
        GET DIAGNOSTICS step_count = ROW_COUNT;
        written := written + step_count;
      END IF;
    END IF;
    INSERT INTO memory.preference_revision_observation(
      owner_user_id, revision_id, observation_id,
      stance, relevance, rationale
    )
    SELECT actor, revision_id_value, link.observation_id,
      link.stance, 1.000, 'memory_projection_v5'
    FROM memory.projection_plan_observation AS link
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS step_count = ROW_COUNT;
    written := written + step_count;
  ELSE
    project_id_value := project_payload.project_id;
    content_value := jsonb_build_object(
      'canonical_text', project_payload.canonical_text,
      'document_state', project_payload.document_state,
      'authority_level', project_payload.authority_level,
      'surface_policy', project_payload.surface_policy::text
    );
    content_sha := memory.v5_digest_text(
      memory.v5_canonical_json_text(content_value)
    );
    IF item.temporal_materialization = 'state_validity_only' THEN
      SELECT stored.* INTO STRICT temporal_row
      FROM memory.observation_temporal AS stored
      WHERE stored.owner_user_id = actor
        AND stored.observation_id = item.temporal_source_observation_id;
    END IF;
    IF action_value = 'create' THEN
      INSERT INTO memory.project_knowledge_head_v5(
        owner_user_id, project_id, component_key, binding_source,
        semantic_key_sha256,
        knowledge_kind, knowledge_key
      ) VALUES (
        actor, project_id_value, project_payload.component_key,
        project_payload.binding_source, item.semantic_key_sha256,
        project_payload.knowledge_kind, project_payload.knowledge_key
      ) RETURNING * INTO project_head;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
      aggregate_id_value := project_head.knowledge_id;
      revision_number_value := 1;
    ELSE
      aggregate_id_value := project_payload.target_knowledge_id;
      revision_number_value := project_head.revision_number;
      revision_id_value := project_head.current_revision_id;
    END IF;
    IF action_value IN ('create', 'revise') THEN
      IF action_value = 'revise' THEN
        revision_number_value := revision_number_value + 1;
      END IF;
      INSERT INTO memory.project_knowledge_revision_v5 AS created_revision(
        owner_user_id, project_id, knowledge_id, revision_number,
        canonical_text, content_sha256, document_state, authority_level,
        surface_policy, prior_revision_id,
        effective_source_observation_id, effective_precision,
        effective_instant_at, effective_calendar_range,
        effective_instant_range, metadata
      ) VALUES (
        actor, project_id_value, aggregate_id_value, revision_number_value,
        project_payload.canonical_text, content_sha,
        project_payload.document_state, project_payload.authority_level,
        project_payload.surface_policy,
        CASE WHEN action_value = 'revise'
          THEN project_head.current_revision_id ELSE NULL END,
        CASE WHEN item.temporal_materialization = 'state_validity_only'
          THEN item.temporal_source_observation_id ELSE NULL END,
        CASE WHEN item.temporal_materialization = 'state_validity_only'
          THEN temporal_row.precision ELSE NULL END,
        CASE WHEN item.temporal_materialization = 'state_validity_only'
          THEN temporal_row.instant_at ELSE NULL END,
        CASE WHEN item.temporal_materialization = 'state_validity_only'
          THEN temporal_row.calendar_range ELSE NULL END,
        CASE WHEN item.temporal_materialization = 'state_validity_only'
          THEN temporal_row.instant_range ELSE NULL END,
        jsonb_build_object(
          'projection_sha256', item.projection_sha256,
          'project_scope', jsonb_build_object(
            'project_id', project_id_value,
            'component_key', project_payload.component_key,
            'binding_source', project_payload.binding_source
          )
        )
      ) RETURNING created_revision.revision_id INTO revision_id_value;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
      UPDATE memory.project_knowledge_head_v5 SET
        current_revision_id = revision_id_value,
        revision_number = revision_number_value,
        updated_at = clock_timestamp()
      WHERE owner_user_id = actor
        AND project_id = project_id_value
        AND knowledge_id = aggregate_id_value;
      GET DIAGNOSTICS step_count = ROW_COUNT;
      written := written + step_count;
    END IF;
    INSERT INTO memory.project_knowledge_revision_observation(
      owner_user_id, project_id, revision_id, observation_id,
      stance, relevance, rationale
    )
    SELECT actor, project_id_value, revision_id_value,
      link.observation_id, link.stance, 1.000, 'memory_projection_v5'
    FROM memory.projection_plan_observation AS link
    WHERE link.owner_user_id = actor
      AND link.plan_id = p_plan_id
      AND link.projection_ref = p_projection_ref
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS step_count = ROW_COUNT;
    written := written + step_count;
  END IF;

  INSERT INTO memory.projection_apply_event(
    owner_user_id, event_id, request_id, plan_id, projection_ref,
    lane, parent_review_state, review_id, review_decision,
    apply_manifest_sha256,
    resulting_claim_id, resulting_claim_revision_number,
    resulting_preference_revision_id,
    resulting_project_id, resulting_project_revision_id,
    outcome, actor_user_id, invoked_by_session
  ) VALUES (
    actor, event_id_value, p_request_id, p_plan_id, p_projection_ref,
    lane_value, review_state_value, p_review_id,
    CASE WHEN review_state_value = 'manual_review_required'
      THEN 'authorized'::memory.projection_review_decision_v5 ELSE NULL END,
    p_apply_manifest_sha256,
    CASE WHEN lane_value = 'claim' THEN aggregate_id_value ELSE NULL END,
    CASE WHEN lane_value = 'claim' THEN revision_number_value ELSE NULL END,
    CASE WHEN lane_value = 'preference' THEN revision_id_value ELSE NULL END,
    CASE WHEN lane_value = 'project_knowledge'
      THEN project_id_value ELSE NULL END,
    CASE WHEN lane_value = 'project_knowledge'
      THEN revision_id_value ELSE NULL END,
    'applied', actor, session_user
  );
  GET DIAGNOSTICS step_count = ROW_COUNT;
  written := written + step_count;

  IF lane_value = 'claim' THEN
    INSERT INTO memory.claim_relation_v5(
      owner_user_id, apply_event_id, from_claim_id, to_claim_id,
      relation_type, reason_code
    )
    SELECT actor, event_id_value, aggregate_id_value,
      relation.target_claim_id, relation.relation_type, relation.reason_code
    FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id = actor
      AND relation.plan_id = p_plan_id
      AND relation.projection_ref = p_projection_ref;
  ELSIF lane_value = 'preference' THEN
    INSERT INTO memory.preference_relation_v5(
      owner_user_id, apply_event_id,
      from_preference_id, to_preference_id, relation_type, reason_code
    )
    SELECT actor, event_id_value, aggregate_id_value,
      relation.target_preference_id,
      relation.relation_type, relation.reason_code
    FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id = actor
      AND relation.plan_id = p_plan_id
      AND relation.projection_ref = p_projection_ref;
  ELSE
    INSERT INTO memory.project_knowledge_relation_v5(
      owner_user_id, apply_event_id, project_id,
      from_knowledge_id, to_knowledge_id, relation_type, reason_code
    )
    SELECT actor, event_id_value, project_id_value, aggregate_id_value,
      relation.target_knowledge_id,
      relation.relation_type, relation.reason_code
    FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id = actor
      AND relation.plan_id = p_plan_id
      AND relation.projection_ref = p_projection_ref;
  END IF;
  GET DIAGNOSTICS step_count = ROW_COUNT;
  written := written + step_count;

  result_value := jsonb_build_object(
    'apply_event_id', event_id_value,
    'lane', lane_value,
    'target_action', action_value,
    'aggregate_id', aggregate_id_value,
    'revision_id', revision_id_value,
    'revision_number', revision_number_value,
    'project_id', project_id_value,
    'component_key', CASE WHEN lane_value = 'project_knowledge'
      THEN component_key_value ELSE NULL END,
    'binding_source', CASE WHEN lane_value = 'project_knowledge'
      THEN binding_source_value ELSE NULL END
  );
  dispatch_payload := jsonb_build_object(
    'contract_version', 'memory_projection_dispatch_v5',
    'operation', 'upsert',
    'owner_user_id', actor,
    'apply_manifest_sha256', p_apply_manifest_sha256,
    'result', result_value
  );
  INSERT INTO memory.projection_dispatch_v5(
    owner_user_id, apply_event_id, lane, operation,
    resulting_claim_id, resulting_claim_revision_number,
    resulting_preference_id, resulting_preference_revision_id,
    resulting_project_id, resulting_knowledge_id,
    resulting_project_revision_id, payload, payload_sha256
  ) VALUES (
    actor, event_id_value, lane_value, 'upsert',
    CASE WHEN lane_value = 'claim' THEN aggregate_id_value ELSE NULL END,
    CASE WHEN lane_value = 'claim' THEN revision_number_value ELSE NULL END,
    CASE WHEN lane_value = 'preference' THEN aggregate_id_value ELSE NULL END,
    CASE WHEN lane_value = 'preference' THEN revision_id_value ELSE NULL END,
    CASE WHEN lane_value = 'project_knowledge'
      THEN project_id_value ELSE NULL END,
    CASE WHEN lane_value = 'project_knowledge'
      THEN aggregate_id_value ELSE NULL END,
    CASE WHEN lane_value = 'project_knowledge'
      THEN revision_id_value ELSE NULL END,
    dispatch_payload,
    memory.v5_digest_text(memory.v5_canonical_json_text(dispatch_payload))
  );
  GET DIAGNOSTICS step_count = ROW_COUNT;
  written := written + step_count;
  RETURN QUERY SELECT event_id_value, 'applied', lane_value,
    aggregate_id_value, revision_id_value, revision_number_value,
    written, result_value;
END
$function$;

DO $ownership$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'claim_relation_v5',
    'preference_relation_v5',
    'project_knowledge_relation_v5',
    'projection_dispatch_v5'
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

ALTER FUNCTION memory.v5_projection_review_manifest_sha256(
  uuid, uuid, text, text, text, integer,
  memory.projection_review_decision_v5, text, text, text, jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_projection_apply_manifest_sha256(
  uuid, text, uuid, text, text, text, memory.projection_lane_v5,
  memory.projection_target_action_v5, integer, uuid, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.projection_apply_state_v5(uuid, text, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_review_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.review_projection_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_apply_v5(uuid, text, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_projection_v5(uuid, uuid, text, uuid, text)
  OWNER TO memory_v5_writer;

GRANT USAGE ON TYPE
  memory.projection_lane_v5,
  memory.projection_target_action_v5,
  memory.projection_review_state_v5,
  memory.projection_review_decision_v5,
  memory.projection_relation_type_v5,
  memory.evidence_stance,
  memory.claim_status
TO memory_v5_writer;

GRANT SELECT, INSERT ON
  memory.claim,
  memory.claim_revision,
  memory.claim_observation,
  memory.claim_relation_v5,
  memory.preference_relation_v5,
  memory.project_knowledge_relation_v5,
  memory.projection_dispatch_v5
TO memory_v5_writer;
GRANT UPDATE (canonical_text, retrieval_policy, metadata)
  ON memory.claim TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.v5_projection_review_manifest_sha256(
  uuid, uuid, text, text, text, integer,
  memory.projection_review_decision_v5, text, text, text, jsonb
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_projection_apply_manifest_sha256(
  uuid, text, uuid, text, text, text, memory.projection_lane_v5,
  memory.projection_target_action_v5, integer, uuid, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.projection_apply_state_v5(uuid, text, uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_projection_review_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.review_projection_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_projection_apply_v5(uuid, text, uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_projection_v5(
  uuid, uuid, text, uuid, text
) FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.v5_projection_review_manifest_sha256(
  uuid, uuid, text, text, text, integer,
  memory.projection_review_decision_v5, text, text, text, jsonb
) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_projection_apply_manifest_sha256(
  uuid, text, uuid, text, text, text, memory.projection_lane_v5,
  memory.projection_target_action_v5, integer, uuid, text
) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.projection_apply_state_v5(uuid, text, uuid)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_review_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_projection_v5(
  uuid, text, memory.projection_review_decision_v5,
  text, text, text, jsonb, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_apply_v5(uuid, text, uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_projection_v5(
  uuid, uuid, text, uuid, text
) TO brains_app;

COMMIT;
