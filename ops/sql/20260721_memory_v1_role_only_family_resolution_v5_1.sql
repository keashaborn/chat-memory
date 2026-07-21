BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure(
       'memory.preflight_entity_resolution_review_v5_1(uuid,memory.entity_review_decision,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.review_entity_resolution_v5_1(uuid,uuid,memory.entity_review_decision,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.1 family-role resolution prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.entity_role_resolution_v5_1 (
  owner_user_id uuid NOT NULL,
  reconciliation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  source_resolution_id uuid NOT NULL,
  successor_resolution_id uuid NOT NULL,
  relationship_role text NOT NULL,
  successor_action memory.entity_resolution_action NOT NULL,
  target_entity_id uuid,
  source_decision_sha256 text NOT NULL,
  mention_sha256 text NOT NULL,
  candidate_set_sha256 text NOT NULL,
  successor_decision_sha256 text NOT NULL,
  reconciliation_manifest_sha256 text NOT NULL,
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, reconciliation_id),
  UNIQUE (owner_user_id, request_id),
  UNIQUE (owner_user_id, source_resolution_id),
  UNIQUE (owner_user_id, successor_resolution_id),
  UNIQUE (owner_user_id, reconciliation_manifest_sha256),
  FOREIGN KEY (owner_user_id, source_resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id, resolution_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, successor_resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id, resolution_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  CHECK (relationship_role IN ('family:mother', 'family:father')),
  CHECK (successor_action IN ('create_new', 'link_existing')),
  CHECK (
    (successor_action = 'create_new' AND target_entity_id IS NULL)
    OR
    (successor_action = 'link_existing' AND target_entity_id IS NOT NULL)
  ),
  CHECK (memory.v5_sha256_valid(source_decision_sha256)),
  CHECK (memory.v5_sha256_valid(mention_sha256)),
  CHECK (memory.v5_sha256_valid(candidate_set_sha256)),
  CHECK (memory.v5_sha256_valid(successor_decision_sha256)),
  CHECK (memory.v5_sha256_valid(reconciliation_manifest_sha256)),
  CHECK (btrim(reason) <> '' AND length(reason) <= 500)
);

CREATE INDEX IF NOT EXISTS entity_role_resolution_v5_1_role_idx
  ON memory.entity_role_resolution_v5_1(
    owner_user_id, relationship_role, created_at DESC
  );

CREATE OR REPLACE FUNCTION memory.preflight_role_only_family_resolution_v5_1(
  p_source_resolution_id uuid,
  p_successor_resolution_id uuid,
  p_reason text
)
RETURNS TABLE(
  source_resolution_id uuid,
  successor_resolution_id uuid,
  relationship_role text,
  successor_action memory.entity_resolution_action,
  target_entity_id uuid,
  proposed_entity jsonb,
  mention_sha256 text,
  candidate_set_sha256 text,
  successor_decision_sha256 text,
  reconciliation_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  source_plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  matching_ids uuid[];
  matching_count integer;
  action_value memory.entity_resolution_action;
  target_value uuid;
  proposed_value jsonb;
  candidate_hash text;
  decision_hash text;
  manifest text;
  role_label text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_source_resolution_id IS NULL OR p_successor_resolution_id IS NULL THEN
    RAISE EXCEPTION 'source and successor resolution IDs are required'
      USING ERRCODE = '22004';
  END IF;
  IF p_source_resolution_id = p_successor_resolution_id THEN
    RAISE EXCEPTION 'successor resolution must differ from source resolution'
      USING ERRCODE = '22023';
  END IF;
  IF btrim(COALESCE(p_reason, '')) = '' OR length(p_reason) > 500 THEN
    RAISE EXCEPTION 'family-role reconciliation reason is required and limited to 500 characters'
      USING ERRCODE = '22023';
  END IF;

  SELECT * INTO source_plan
  FROM memory.entity_resolution_plan AS plan
  WHERE plan.owner_user_id = actor
    AND plan.resolution_id = p_source_resolution_id;
  IF NOT FOUND
     OR source_plan.predicate_registry_version NOT IN (
       'memory_predicate_registry_v5', 'memory_predicate_registry_v5_1'
     )
     OR source_plan.entity_normalization_version <> 'memory_entity_normalization_v5'
     OR source_plan.action <> 'defer'
     OR source_plan.decision_state <> 'deferred' THEN
    RAISE EXCEPTION 'owner-scoped deferred role-only resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.entity_resolution_apply AS applied
    JOIN memory.entity_resolution_plan AS applied_plan
      ON applied_plan.owner_user_id = applied.owner_user_id
     AND applied_plan.resolution_id = applied.resolution_id
    WHERE applied_plan.owner_user_id = actor
      AND applied_plan.mention_id = source_plan.mention_id
  ) THEN
    RAISE EXCEPTION 'mention already has an applied resolution'
      USING ERRCODE = '23505';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan AS successor
    WHERE successor.resolution_id = p_successor_resolution_id
  ) THEN
    RAISE EXCEPTION 'successor resolution ID is already in use'
      USING ERRCODE = '23505';
  END IF;

  SELECT * INTO STRICT mention
  FROM memory.entity_mention AS staged_mention
  WHERE staged_mention.owner_user_id = actor
    AND staged_mention.mention_id = source_plan.mention_id
    AND staged_mention.evidence_id = source_plan.evidence_id;
  IF mention.entity_type <> 'person'
     OR mention.mention_kind <> 'role_only'
     OR mention.name_text IS NOT NULL
     OR mention.relationship_role NOT IN ('family:mother', 'family:father') THEN
    RAISE EXCEPTION 'only closed, non-repeatable parent roles may use this contract'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT evidence
  FROM memory.evidence AS source_evidence
  WHERE source_evidence.owner_user_id = actor
    AND source_evidence.evidence_id = source_plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'family-role reconciliation requires active evidence'
      USING ERRCODE = '23514';
  END IF;

  SELECT array_agg(candidate.entity_id ORDER BY candidate.entity_id)
  INTO matching_ids
  FROM (
    SELECT owner_entity.entity_id
    FROM memory.entity AS owner_entity
    WHERE owner_entity.owner_user_id = actor
      AND owner_entity.status = 'active'
      AND owner_entity.entity_type = 'person'
      AND owner_entity.metadata->>'identity_state' = 'role_only'
      AND owner_entity.metadata->>'relationship_role' = mention.relationship_role
    UNION
    SELECT applied.applied_entity_id
    FROM memory.entity_mention AS prior_mention
    JOIN memory.entity_resolution_plan AS prior_plan
      ON prior_plan.owner_user_id = prior_mention.owner_user_id
     AND prior_plan.mention_id = prior_mention.mention_id
    JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id = prior_plan.owner_user_id
     AND applied.resolution_id = prior_plan.resolution_id
    JOIN memory.entity AS prior_target
      ON prior_target.owner_user_id = applied.owner_user_id
     AND prior_target.entity_id = applied.applied_entity_id
    WHERE prior_mention.owner_user_id = actor
      AND prior_mention.entity_type = 'person'
      AND prior_mention.relationship_role = mention.relationship_role
      AND prior_target.status = 'active'
  ) AS candidate;
  matching_count := COALESCE(cardinality(matching_ids), 0);
  IF matching_count > 1 THEN
    RAISE EXCEPTION 'owner role history is ambiguous'
      USING ERRCODE = '23514';
  END IF;

  role_label := split_part(mention.relationship_role, ':', 2);
  IF matching_count = 1 THEN
    action_value := 'link_existing';
    target_value := matching_ids[1];
    proposed_value := NULL;
  ELSE
    action_value := 'create_new';
    target_value := NULL;
    proposed_value := jsonb_build_object(
      'entity_type', 'person',
      'display_label', initcap(role_label),
      'canonical_name', NULL,
      'identity_state', 'role_only',
      'creation_reason', 'reviewed_unique_owner_family_role'
    );
  END IF;
  candidate_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_role_only_family_candidate_v5_1', actor::text,
    source_plan.resolution_id::text, p_successor_resolution_id::text,
    mention.mention_sha256, mention.relationship_role,
    action_value::text, COALESCE(target_value::text, ''),
    COALESCE(proposed_value::text, '')
  ));
  decision_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_role_only_family_decision_v5_1', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    p_successor_resolution_id::text, mention.relationship_role,
    action_value::text, COALESCE(target_value::text, ''),
    COALESCE(proposed_value::text, ''), candidate_hash,
    'manual_review_required'
  ));
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_role_only_family_resolution_v5_1', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    evidence.content_sha256, mention.mention_sha256,
    p_successor_resolution_id::text, mention.relationship_role,
    action_value::text, COALESCE(target_value::text, ''),
    COALESCE(proposed_value::text, ''), candidate_hash, decision_hash,
    btrim(p_reason)
  ));
  RETURN QUERY SELECT
    source_plan.resolution_id, p_successor_resolution_id,
    mention.relationship_role, action_value, target_value,
    proposed_value, mention.mention_sha256,
    candidate_hash, decision_hash, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.reconcile_role_only_family_resolution_v5_1(
  p_request_id uuid,
  p_source_resolution_id uuid,
  p_successor_resolution_id uuid,
  p_reason text,
  p_reconciliation_manifest_sha256 text
)
RETURNS TABLE(
  reconciliation_id uuid,
  successor_resolution_id uuid,
  successor_action memory.entity_resolution_action,
  target_entity_id uuid,
  outcome text,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  source_plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  existing memory.entity_role_resolution_v5_1%ROWTYPE;
  reconciliation_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL THEN
    RAISE EXCEPTION 'request_id is required' USING ERRCODE = '22004';
  END IF;
  SELECT * INTO existing
  FROM memory.entity_role_resolution_v5_1 AS prior
  WHERE prior.owner_user_id = actor AND prior.request_id = p_request_id;
  IF FOUND THEN
    IF existing.source_resolution_id <> p_source_resolution_id
       OR existing.successor_resolution_id <> p_successor_resolution_id
       OR existing.reconciliation_manifest_sha256 <>
          p_reconciliation_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    result_value := jsonb_build_object(
      'reconciliation_id', existing.reconciliation_id,
      'source_resolution_id', existing.source_resolution_id,
      'successor_resolution_id', existing.successor_resolution_id,
      'successor_action', existing.successor_action,
      'target_entity_id', existing.target_entity_id
    );
    RETURN QUERY SELECT existing.reconciliation_id,
      existing.successor_resolution_id, existing.successor_action,
      existing.target_entity_id, 'replayed'::text, result_value;
    RETURN;
  END IF;

  PERFORM 1 FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_source_resolution_id
  FOR UPDATE;
  SELECT * INTO source_plan FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_source_resolution_id;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|mention|' || source_plan.mention_id::text, 0
  ));
  PERFORM 1 FROM memory.evidence
  WHERE owner_user_id = actor AND evidence_id = source_plan.evidence_id
  FOR UPDATE;
  SELECT * INTO preflight
  FROM memory.preflight_role_only_family_resolution_v5_1(
    p_source_resolution_id, p_successor_resolution_id, p_reason
  );
  IF p_reconciliation_manifest_sha256 <>
     preflight.reconciliation_manifest_sha256 THEN
    RAISE EXCEPTION 'family-role reconciliation manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT mention FROM memory.entity_mention
  WHERE owner_user_id = actor AND mention_id = source_plan.mention_id;

  INSERT INTO memory.entity_resolution_plan(
    resolution_id, owner_user_id, evidence_id, mention_id,
    predicate_registry_version, entity_normalization_version,
    resolver, resolver_version, action, decision_state,
    selected_entity_id, proposed_entity, candidate_set_sha256,
    decision_sha256, review_reason_codes
  ) VALUES (
    p_successor_resolution_id, actor, source_plan.evidence_id,
    source_plan.mention_id, 'memory_predicate_registry_v5_1',
    'memory_entity_normalization_v5',
    'governed_role_resolution_reconciliation',
    'memory_v1_role_only_family_resolution_v5_1',
    preflight.successor_action, 'manual_review_required',
    preflight.target_entity_id, preflight.proposed_entity,
    preflight.candidate_set_sha256,
    preflight.successor_decision_sha256,
    '["role_only_family_resolution_requires_review"]'::jsonb
  );
  IF preflight.successor_action = 'link_existing' THEN
    INSERT INTO memory.entity_resolution_candidate(
      owner_user_id, resolution_id, candidate_entity_id,
      ordinal, entity_type, features, exclusion_reasons
    ) VALUES (
      actor, p_successor_resolution_id, preflight.target_entity_id,
      1, 'person',
      jsonb_build_object(
        'active_status', true,
        'entity_type_match', true,
        'exact_canonical_name', false,
        'exact_alias', false,
        'relationship_role_supported', true,
        'source_local_coreference', false,
        'graph_neighbor_supported', true,
        'conflicting_attribute_count', 0,
        'same_name_candidate_count', 0
      ),
      '[]'::jsonb
    );
  END IF;

  INSERT INTO memory.entity_role_resolution_v5_1(
    owner_user_id, reconciliation_id, request_id,
    source_resolution_id, successor_resolution_id,
    relationship_role, successor_action, target_entity_id,
    source_decision_sha256, mention_sha256,
    candidate_set_sha256, successor_decision_sha256,
    reconciliation_manifest_sha256, reason
  ) VALUES (
    actor, reconciliation_id_value, p_request_id,
    p_source_resolution_id, p_successor_resolution_id,
    preflight.relationship_role, preflight.successor_action,
    preflight.target_entity_id, source_plan.decision_sha256,
    preflight.mention_sha256, preflight.candidate_set_sha256,
    preflight.successor_decision_sha256,
    p_reconciliation_manifest_sha256, btrim(p_reason)
  );
  result_value := jsonb_build_object(
    'reconciliation_id', reconciliation_id_value,
    'source_resolution_id', p_source_resolution_id,
    'successor_resolution_id', p_successor_resolution_id,
    'successor_action', preflight.successor_action,
    'target_entity_id', preflight.target_entity_id
  );
  RETURN QUERY SELECT reconciliation_id_value,
    p_successor_resolution_id, preflight.successor_action,
    preflight.target_entity_id, 'applied'::text, result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_role_only_family_apply_v5_1(
  p_resolution_id uuid,
  p_review_id uuid
)
RETURNS TABLE(
  resolution_id uuid,
  action memory.entity_resolution_action,
  review_id uuid,
  relationship_role text,
  prospective_entity_id uuid,
  entity_state_sha256 text,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  review memory.entity_resolution_review%ROWTYPE;
  entity_row memory.entity%ROWTYPE;
  entity_state text;
  matching_count integer;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT * INTO plan FROM memory.entity_resolution_plan AS staged_plan
  WHERE staged_plan.owner_user_id = actor
    AND staged_plan.resolution_id = p_resolution_id;
  IF NOT FOUND
     OR plan.predicate_registry_version <> 'memory_predicate_registry_v5_1'
     OR plan.resolver_version <> 'memory_v1_role_only_family_resolution_v5_1'
     OR plan.decision_state <> 'manual_review_required'
     OR plan.action NOT IN ('create_new', 'link_existing') THEN
    RAISE EXCEPTION 'owner-scoped reviewed family-role resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT * INTO STRICT mention FROM memory.entity_mention
  WHERE owner_user_id = actor AND mention_id = plan.mention_id;
  IF mention.mention_kind <> 'role_only'
     OR mention.entity_type <> 'person'
     OR mention.relationship_role NOT IN ('family:mother', 'family:father') THEN
    RAISE EXCEPTION 'family-role mention contract drifted'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT evidence FROM memory.evidence
  WHERE owner_user_id = actor AND evidence_id = plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'family-role apply requires active evidence'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO review FROM memory.entity_resolution_review AS approved_review
  WHERE approved_review.owner_user_id = actor
    AND approved_review.resolution_id = p_resolution_id
    AND approved_review.review_id = p_review_id;
  IF NOT FOUND OR review.decision <> 'approved'
     OR review.review_id IS DISTINCT FROM (
       SELECT latest.review_id
       FROM memory.entity_resolution_review AS latest
       WHERE latest.owner_user_id = actor
         AND latest.resolution_id = p_resolution_id
       ORDER BY latest.created_at DESC, latest.review_id DESC
       LIMIT 1
     ) THEN
    RAISE EXCEPTION 'latest family-role review is not the supplied approval'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(DISTINCT candidate.entity_id)
  INTO matching_count
  FROM (
    SELECT owner_entity.entity_id
    FROM memory.entity AS owner_entity
    WHERE owner_entity.owner_user_id = actor
      AND owner_entity.status = 'active'
      AND owner_entity.entity_type = 'person'
      AND owner_entity.metadata->>'relationship_role' = mention.relationship_role
    UNION
    SELECT applied.applied_entity_id
    FROM memory.entity_mention AS prior_mention
    JOIN memory.entity_resolution_plan AS prior_plan
      ON prior_plan.owner_user_id = prior_mention.owner_user_id
     AND prior_plan.mention_id = prior_mention.mention_id
    JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id = prior_plan.owner_user_id
     AND applied.resolution_id = prior_plan.resolution_id
    WHERE prior_mention.owner_user_id = actor
      AND prior_mention.entity_type = 'person'
      AND prior_mention.relationship_role = mention.relationship_role
  ) AS candidate;

  IF plan.action = 'create_new' THEN
    IF matching_count <> 0
       OR plan.selected_entity_id IS NOT NULL
       OR plan.proposed_entity->>'identity_state' <> 'role_only'
       OR plan.proposed_entity->>'entity_type' <> 'person'
       OR plan.proposed_entity->'canonical_name' <> 'null'::jsonb
       OR plan.proposed_entity ? 'relationship_role'
       OR plan.proposed_entity->>'display_label'
          <> initcap(split_part(mention.relationship_role, ':', 2)) THEN
      RAISE EXCEPTION 'family-role create target drifted or now conflicts'
        USING ERRCODE = '23514';
    END IF;
    entity_state := plan.proposed_entity::text;
    prospective_entity_id := NULL;
  ELSE
    IF matching_count <> 1 OR plan.proposed_entity IS NOT NULL THEN
      RAISE EXCEPTION 'family-role link target is no longer unique'
        USING ERRCODE = '23514';
    END IF;
    SELECT * INTO entity_row FROM memory.entity
    WHERE owner_user_id = actor AND entity_id = plan.selected_entity_id;
    IF NOT FOUND OR entity_row.status <> 'active'
       OR entity_row.entity_type <> 'person' THEN
      RAISE EXCEPTION 'family-role selected entity is not active'
        USING ERRCODE = '23514';
    END IF;
    entity_state := concat_ws('|',
      entity_row.entity_id::text, entity_row.entity_key,
      entity_row.entity_type, entity_row.canonical_name,
      entity_row.normalized_name, entity_row.status::text,
      entity_row.updated_at::text, mention.relationship_role
    );
    prospective_entity_id := entity_row.entity_id;
  END IF;
  entity_state := memory.v5_digest_text(entity_state);
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_role_only_family_apply_v5_1', actor::text,
    p_resolution_id::text, evidence.content_sha256,
    mention.mention_sha256, plan.candidate_set_sha256,
    plan.decision_sha256, plan.action::text,
    p_review_id::text, review.authorization_manifest_sha256,
    mention.relationship_role, entity_state
  ));
  RETURN QUERY SELECT plan.resolution_id, plan.action,
    p_review_id, mention.relationship_role,
    prospective_entity_id, entity_state, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_role_only_family_resolution_v5_1(
  p_request_id uuid,
  p_resolution_id uuid,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  applied_entity_id uuid,
  outcome text,
  bindings_created integer,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_apply memory.entity_resolution_apply%ROWTYPE;
  entity_id_value uuid;
  normalized_name text;
  binding_count integer := 0;
  observation_row memory.observation%ROWTYPE;
  subject_resolution uuid;
  subject_entity uuid;
  object_resolution uuid;
  object_entity uuid;
  binding_hash text;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL THEN
    RAISE EXCEPTION 'request_id is required' USING ERRCODE = '22004';
  END IF;
  PERFORM 1 FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id
  FOR UPDATE;
  SELECT * INTO plan FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id;
  SELECT * INTO mention FROM memory.entity_mention
  WHERE owner_user_id = actor AND mention_id = plan.mention_id;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|family-role|' || mention.relationship_role, 0
  ));
  PERFORM 1 FROM memory.evidence
  WHERE owner_user_id = actor AND evidence_id = plan.evidence_id
  FOR UPDATE;
  PERFORM 1 FROM memory.entity_resolution_review
  WHERE owner_user_id = actor
    AND resolution_id = p_resolution_id
    AND review_id = p_review_id
  FOR UPDATE;
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor AND request.request_id = p_request_id;
  IF FOUND THEN
    IF existing_request.operation <> 'apply_resolution'
       OR existing_request.target_key <> p_resolution_id::text
       OR existing_request.manifest_sha256 <> p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'applied_entity_id')::uuid,
      'replayed'::text, 0, existing_request.result;
    RETURN;
  END IF;
  SELECT * INTO existing_apply FROM memory.entity_resolution_apply
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id;
  IF FOUND THEN
    IF existing_apply.apply_manifest_sha256 <> p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'applied family-role resolution manifest mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT existing_apply.applied_entity_id,
      'replayed'::text, 0, jsonb_build_object(
        'resolution_id', p_resolution_id,
        'applied_entity_id', existing_apply.applied_entity_id,
        'bindings_created', 0
    );
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_role_only_family_apply_v5_1(
    p_resolution_id, p_review_id
  );
  IF p_apply_manifest_sha256 <> preflight.apply_manifest_sha256 THEN
    RAISE EXCEPTION 'family-role apply manifest mismatch'
      USING ERRCODE = '23514';
  END IF;

  IF plan.action = 'link_existing' THEN
    entity_id_value := plan.selected_entity_id;
  ELSE
    normalized_name := memory.normalize_entity_name_v5(
      split_part(mention.relationship_role, ':', 2)
    );
    entity_id_value := gen_random_uuid();
    INSERT INTO memory.entity(
      entity_id, owner_user_id, entity_key, entity_type,
      canonical_name, normalized_name, metadata
    ) VALUES (
      entity_id_value, actor,
      'ent_' || replace(gen_random_uuid()::text, '-', ''),
      'person', split_part(mention.relationship_role, ':', 2), normalized_name,
      jsonb_build_object(
        'origin', 'memory_v1_relational_v5',
        'identity_state', 'role_only',
        'relationship_role', mention.relationship_role,
        'resolution_id', p_resolution_id
      )
    );
  END IF;

  INSERT INTO memory.entity_resolution_apply(
    owner_user_id, resolution_id, review_id,
    applied_entity_id, apply_manifest_sha256
  ) VALUES (
    actor, p_resolution_id, p_review_id,
    entity_id_value, p_apply_manifest_sha256
  );

  FOR observation_row IN
    SELECT * FROM memory.observation AS observation
    WHERE observation.owner_user_id = actor
      AND (
        observation.subject_mention_id = mention.mention_id
        OR observation.object_mention_id = mention.mention_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.observation_entity_binding AS binding
        WHERE binding.owner_user_id = observation.owner_user_id
          AND binding.observation_id = observation.observation_id
      )
  LOOP
    SELECT applied.resolution_id, applied.applied_entity_id
    INTO subject_resolution, subject_entity
    FROM memory.entity_resolution_plan AS subject_plan
    JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id = subject_plan.owner_user_id
     AND applied.resolution_id = subject_plan.resolution_id
    WHERE subject_plan.owner_user_id = actor
      AND subject_plan.mention_id = observation_row.subject_mention_id;
    IF NOT FOUND THEN
      CONTINUE;
    END IF;
    object_resolution := NULL;
    object_entity := NULL;
    IF observation_row.object_mention_id IS NOT NULL THEN
      SELECT applied.resolution_id, applied.applied_entity_id
      INTO object_resolution, object_entity
      FROM memory.entity_resolution_plan AS object_plan
      JOIN memory.entity_resolution_apply AS applied
        ON applied.owner_user_id = object_plan.owner_user_id
       AND applied.resolution_id = object_plan.resolution_id
      WHERE object_plan.owner_user_id = actor
        AND object_plan.mention_id = observation_row.object_mention_id;
      IF NOT FOUND THEN
        CONTINUE;
      END IF;
    END IF;
    binding_hash := memory.v5_digest_text(concat_ws('|',
      observation_row.observation_sha256,
      subject_resolution::text, subject_entity::text,
      COALESCE(object_resolution::text, ''),
      COALESCE(object_entity::text, '')
    ));
    INSERT INTO memory.observation_entity_binding(
      owner_user_id, observation_id,
      subject_resolution_id, subject_entity_id,
      object_resolution_id, object_entity_id,
      binding_manifest_sha256
    ) VALUES (
      actor, observation_row.observation_id,
      subject_resolution, subject_entity,
      object_resolution, object_entity, binding_hash
    );
    binding_count := binding_count + 1;
  END LOOP;

  result_value := jsonb_build_object(
    'resolution_id', p_resolution_id,
    'relationship_role', mention.relationship_role,
    'applied_entity_id', entity_id_value,
    'bindings_created', binding_count
  );
  INSERT INTO memory.relational_operation_request(
    request_id, owner_user_id, operation, target_key,
    manifest_sha256, outcome, result, invoked_by_session
  ) VALUES (
    p_request_id, actor, 'apply_resolution',
    p_resolution_id::text, p_apply_manifest_sha256,
    'applied', result_value, session_user
  );
  RETURN QUERY SELECT entity_id_value, 'applied'::text,
    binding_count, result_value;
END
$function$;

ALTER TABLE memory.entity_role_resolution_v5_1
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_role_only_family_resolution_v5_1(
  uuid, uuid, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.reconcile_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_role_only_family_apply_v5_1(uuid, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text
) OWNER TO memory_v5_writer;

ALTER TABLE memory.entity_role_resolution_v5_1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entity_role_resolution_v5_1 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.entity_role_resolution_v5_1;
CREATE POLICY owner_isolation
  ON memory.entity_role_resolution_v5_1
  TO memory_v5_writer
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS entity_role_resolution_v5_1_append_only_guard
  ON memory.entity_role_resolution_v5_1;
CREATE TRIGGER entity_role_resolution_v5_1_append_only_guard
BEFORE UPDATE OR DELETE ON memory.entity_role_resolution_v5_1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

REVOKE ALL ON memory.entity_role_resolution_v5_1
  FROM PUBLIC, brains_app;
GRANT SELECT, INSERT ON memory.entity_role_resolution_v5_1
  TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_role_only_family_resolution_v5_1(
  uuid, uuid, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.reconcile_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_role_only_family_apply_v5_1(uuid, uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text
) FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_role_only_family_resolution_v5_1(
  uuid, uuid, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.reconcile_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_role_only_family_apply_v5_1(uuid, uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text
) TO brains_app;

COMMIT;
