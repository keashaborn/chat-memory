BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure(
       'memory.preflight_entity_resolution_review_v5(uuid,memory.entity_review_decision,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_entity_resolution_v5(uuid,uuid,uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.1 entity reconciliation prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.entity_resolution_reconciliation_v5_1 (
  owner_user_id uuid NOT NULL,
  reconciliation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  source_resolution_id uuid NOT NULL,
  successor_resolution_id uuid NOT NULL,
  target_entity_id uuid NOT NULL,
  match_basis text NOT NULL,
  source_decision_sha256 text NOT NULL,
  mention_sha256 text NOT NULL,
  target_state_sha256 text NOT NULL,
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
  CHECK (match_basis = 'unique_owner_role_history'),
  CHECK (memory.v5_sha256_valid(source_decision_sha256)),
  CHECK (memory.v5_sha256_valid(mention_sha256)),
  CHECK (memory.v5_sha256_valid(target_state_sha256)),
  CHECK (memory.v5_sha256_valid(candidate_set_sha256)),
  CHECK (memory.v5_sha256_valid(successor_decision_sha256)),
  CHECK (memory.v5_sha256_valid(reconciliation_manifest_sha256)),
  CHECK (btrim(reason) <> '' AND length(reason) <= 500)
);

CREATE INDEX IF NOT EXISTS entity_resolution_reconciliation_v5_1_target_idx
  ON memory.entity_resolution_reconciliation_v5_1(
    owner_user_id, target_entity_id, created_at DESC
  );

CREATE OR REPLACE FUNCTION memory.preflight_entity_resolution_reconciliation_v5_1(
  p_source_resolution_id uuid,
  p_successor_resolution_id uuid,
  p_target_entity_id uuid,
  p_reason text
)
RETURNS TABLE(
  source_resolution_id uuid,
  successor_resolution_id uuid,
  target_entity_id uuid,
  source_action memory.entity_resolution_action,
  source_decision_state memory.entity_resolution_state,
  match_basis text,
  mention_sha256 text,
  target_state_sha256 text,
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
  target memory.entity%ROWTYPE;
  supported_target_count integer;
  target_state text;
  candidate_hash text;
  decision_hash text;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_source_resolution_id IS NULL
     OR p_successor_resolution_id IS NULL
     OR p_target_entity_id IS NULL THEN
    RAISE EXCEPTION 'source, successor, and target IDs are required'
      USING ERRCODE = '22004';
  END IF;
  IF p_source_resolution_id = p_successor_resolution_id THEN
    RAISE EXCEPTION 'successor resolution must differ from source resolution'
      USING ERRCODE = '22023';
  END IF;
  IF btrim(COALESCE(p_reason, '')) = '' OR length(p_reason) > 500 THEN
    RAISE EXCEPTION 'reconciliation reason is required and limited to 500 characters'
      USING ERRCODE = '22023';
  END IF;

  SELECT * INTO source_plan
  FROM memory.entity_resolution_plan AS plan
  WHERE plan.owner_user_id = actor
    AND plan.resolution_id = p_source_resolution_id;
  IF NOT FOUND
     OR source_plan.predicate_registry_version <> 'memory_predicate_registry_v5_1'
     OR source_plan.entity_normalization_version <> 'memory_entity_normalization_v5'
     OR source_plan.action <> 'create_new'
     OR source_plan.decision_state <> 'manual_review_required' THEN
    RAISE EXCEPTION 'owner-scoped V5.1 create-new review plan not found'
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
  IF mention.mention_kind <> 'named'
     OR btrim(COALESCE(mention.relationship_role, '')) = ''
     OR mention.relationship_role = 'user:self' THEN
    RAISE EXCEPTION 'reconciliation requires a named non-self relationship role'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT evidence
  FROM memory.evidence AS source_evidence
  WHERE source_evidence.owner_user_id = actor
    AND source_evidence.evidence_id = source_plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'reconciliation requires active evidence'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO target
  FROM memory.entity AS owner_entity
  WHERE owner_entity.owner_user_id = actor
    AND owner_entity.entity_id = p_target_entity_id;
  IF NOT FOUND OR target.status <> 'active'
     OR target.entity_type <> mention.entity_type THEN
    RAISE EXCEPTION 'target must be an active same-type owner entity'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(DISTINCT applied.applied_entity_id)
  INTO supported_target_count
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
    AND prior_mention.relationship_role = mention.relationship_role
    AND prior_mention.entity_type = mention.entity_type
    AND prior_target.status = 'active';
  IF supported_target_count <> 1 OR NOT EXISTS (
    SELECT 1
    FROM memory.entity_mention AS prior_mention
    JOIN memory.entity_resolution_plan AS prior_plan
      ON prior_plan.owner_user_id = prior_mention.owner_user_id
     AND prior_plan.mention_id = prior_mention.mention_id
    JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id = prior_plan.owner_user_id
     AND applied.resolution_id = prior_plan.resolution_id
    WHERE prior_mention.owner_user_id = actor
      AND prior_mention.relationship_role = mention.relationship_role
      AND prior_mention.entity_type = mention.entity_type
      AND applied.applied_entity_id = p_target_entity_id
  ) THEN
    RAISE EXCEPTION 'target is not uniquely supported by owner role history'
      USING ERRCODE = '23514';
  END IF;

  target_state := memory.v5_digest_text(concat_ws('|',
    target.entity_id::text, target.entity_key, target.entity_type,
    target.canonical_name, target.normalized_name,
    target.status::text, target.updated_at::text,
    mention.relationship_role, supported_target_count::text
  ));
  candidate_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_entity_reconciliation_candidate_v5_1', actor::text,
    source_plan.resolution_id::text, p_successor_resolution_id::text,
    p_target_entity_id::text, mention.mention_sha256,
    mention.relationship_role, target_state,
    'unique_owner_role_history'
  ));
  decision_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_entity_reconciliation_decision_v5_1', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    p_successor_resolution_id::text, p_target_entity_id::text,
    candidate_hash, 'link_existing', 'manual_review_required'
  ));
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_entity_reconciliation_v5_1', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    evidence.content_sha256, mention.mention_sha256,
    p_successor_resolution_id::text, p_target_entity_id::text,
    target_state, candidate_hash, decision_hash,
    'unique_owner_role_history', btrim(p_reason)
  ));
  RETURN QUERY SELECT
    source_plan.resolution_id, p_successor_resolution_id, target.entity_id,
    source_plan.action, source_plan.decision_state,
    'unique_owner_role_history'::text, mention.mention_sha256,
    target_state, candidate_hash, decision_hash, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.reconcile_entity_resolution_v5_1(
  p_request_id uuid,
  p_source_resolution_id uuid,
  p_successor_resolution_id uuid,
  p_target_entity_id uuid,
  p_reason text,
  p_reconciliation_manifest_sha256 text
)
RETURNS TABLE(
  reconciliation_id uuid,
  successor_resolution_id uuid,
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
  existing memory.entity_resolution_reconciliation_v5_1%ROWTYPE;
  reconciliation_id_value uuid := gen_random_uuid();
  features jsonb;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL THEN
    RAISE EXCEPTION 'request_id is required' USING ERRCODE = '22004';
  END IF;
  SELECT * INTO existing
  FROM memory.entity_resolution_reconciliation_v5_1 AS prior
  WHERE prior.owner_user_id = actor AND prior.request_id = p_request_id;
  IF FOUND THEN
    IF existing.source_resolution_id <> p_source_resolution_id
       OR existing.successor_resolution_id <> p_successor_resolution_id
       OR existing.target_entity_id <> p_target_entity_id
       OR existing.reconciliation_manifest_sha256 <>
          p_reconciliation_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    result_value := jsonb_build_object(
      'reconciliation_id', existing.reconciliation_id,
      'source_resolution_id', existing.source_resolution_id,
      'successor_resolution_id', existing.successor_resolution_id,
      'target_entity_id', existing.target_entity_id
    );
    RETURN QUERY SELECT existing.reconciliation_id,
      existing.successor_resolution_id, existing.target_entity_id,
      'replayed'::text, result_value;
    RETURN;
  END IF;
  SELECT * INTO existing
  FROM memory.entity_resolution_reconciliation_v5_1 AS prior
  WHERE prior.owner_user_id = actor
    AND prior.reconciliation_manifest_sha256 = p_reconciliation_manifest_sha256;
  IF FOUND THEN
    result_value := jsonb_build_object(
      'reconciliation_id', existing.reconciliation_id,
      'source_resolution_id', existing.source_resolution_id,
      'successor_resolution_id', existing.successor_resolution_id,
      'target_entity_id', existing.target_entity_id
    );
    RETURN QUERY SELECT existing.reconciliation_id,
      existing.successor_resolution_id, existing.target_entity_id,
      'replayed'::text, result_value;
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
  PERFORM 1 FROM memory.entity
  WHERE owner_user_id = actor AND entity_id = p_target_entity_id
  FOR UPDATE;
  SELECT * INTO preflight
  FROM memory.preflight_entity_resolution_reconciliation_v5_1(
    p_source_resolution_id, p_successor_resolution_id,
    p_target_entity_id, p_reason
  );
  IF p_reconciliation_manifest_sha256 <>
     preflight.reconciliation_manifest_sha256 THEN
    RAISE EXCEPTION 'reconciliation manifest mismatch'
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
    'governed_role_history_reconciliation',
    'memory_v1_entity_resolution_reconciliation_v5_1',
    'link_existing', 'manual_review_required', p_target_entity_id, NULL,
    preflight.candidate_set_sha256, preflight.successor_decision_sha256,
    '["owner_local_exact_relationship_role_history",
      "manual_reconciliation_required"]'::jsonb
  );
  features := jsonb_build_object(
    'active_status', true,
    'entity_type_match', true,
    'exact_canonical_name',
      memory.normalize_entity_name_v5(mention.name_text) = (
        SELECT normalized_name FROM memory.entity
        WHERE owner_user_id = actor AND entity_id = p_target_entity_id
      ),
    'exact_alias', EXISTS (
      SELECT 1 FROM memory.entity_alias_observation AS alias
      WHERE alias.owner_user_id = actor
        AND alias.entity_id = p_target_entity_id
        AND alias.normalized_alias = memory.normalize_entity_name_v5(mention.name_text)
    ),
    'relationship_role_supported', true,
    'source_local_coreference', false,
    'graph_neighbor_supported', true,
    'conflicting_attribute_count', 0,
    'same_name_candidate_count', 0
  );
  INSERT INTO memory.entity_resolution_candidate(
    owner_user_id, resolution_id, candidate_entity_id,
    ordinal, entity_type, features, exclusion_reasons
  ) VALUES (
    actor, p_successor_resolution_id, p_target_entity_id, 1,
    mention.entity_type, features,
    CASE
      WHEN (features->>'exact_canonical_name')::boolean
        OR (features->>'exact_alias')::boolean THEN '[]'::jsonb
      ELSE '["name_mismatch_requires_governed_review"]'::jsonb
    END
  );
  INSERT INTO memory.entity_resolution_reconciliation_v5_1(
    owner_user_id, reconciliation_id, request_id,
    source_resolution_id, successor_resolution_id, target_entity_id,
    match_basis, source_decision_sha256, mention_sha256,
    target_state_sha256, candidate_set_sha256,
    successor_decision_sha256, reconciliation_manifest_sha256, reason
  ) VALUES (
    actor, reconciliation_id_value, p_request_id,
    p_source_resolution_id, p_successor_resolution_id, p_target_entity_id,
    preflight.match_basis, source_plan.decision_sha256,
    preflight.mention_sha256, preflight.target_state_sha256,
    preflight.candidate_set_sha256, preflight.successor_decision_sha256,
    p_reconciliation_manifest_sha256, btrim(p_reason)
  );
  result_value := jsonb_build_object(
    'reconciliation_id', reconciliation_id_value,
    'source_resolution_id', p_source_resolution_id,
    'successor_resolution_id', p_successor_resolution_id,
    'target_entity_id', p_target_entity_id
  );
  RETURN QUERY SELECT reconciliation_id_value,
    p_successor_resolution_id, p_target_entity_id,
    'applied'::text, result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_entity_resolution_review_v5_1(
  p_resolution_id uuid,
  p_decision memory.entity_review_decision,
  p_reason text
)
RETURNS TABLE(
  resolution_id uuid,
  action memory.entity_resolution_action,
  decision_state memory.entity_resolution_state,
  mention_sha256 text,
  candidate_set_sha256 text,
  decision_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
  PERFORM memory.require_v5_writer_context();
  IF NOT EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan AS plan
    WHERE plan.owner_user_id = memory.current_actor_user_id()
      AND plan.resolution_id = p_resolution_id
      AND plan.predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'owner-scoped V5.1 resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  RETURN QUERY SELECT *
  FROM memory.preflight_entity_resolution_review_v5(
    p_resolution_id, p_decision, p_reason
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_entity_resolution_v5_1(
  p_request_id uuid,
  p_resolution_id uuid,
  p_decision memory.entity_review_decision,
  p_reason text,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(review_id uuid, outcome text, result jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
  PERFORM memory.require_v5_writer_context();
  IF NOT EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan AS plan
    WHERE plan.owner_user_id = memory.current_actor_user_id()
      AND plan.resolution_id = p_resolution_id
      AND plan.predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'owner-scoped V5.1 resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  RETURN QUERY SELECT * FROM memory.review_entity_resolution_v5(
    p_request_id, p_resolution_id, p_decision,
    p_reason, p_authorization_manifest_sha256
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_entity_resolution_apply_v5_1(
  p_resolution_id uuid,
  p_review_id uuid DEFAULT NULL
)
RETURNS TABLE(
  resolution_id uuid,
  action memory.entity_resolution_action,
  decision_state memory.entity_resolution_state,
  review_id uuid,
  prospective_entity_id uuid,
  entity_state_sha256 text,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
  PERFORM memory.require_v5_writer_context();
  IF NOT EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan AS plan
    WHERE plan.owner_user_id = memory.current_actor_user_id()
      AND plan.resolution_id = p_resolution_id
      AND plan.predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'owner-scoped V5.1 resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  RETURN QUERY SELECT *
  FROM memory.preflight_entity_resolution_apply_v5(
    p_resolution_id, p_review_id
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_entity_resolution_v5_1(
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
BEGIN
  PERFORM memory.require_v5_writer_context();
  IF NOT EXISTS (
    SELECT 1 FROM memory.entity_resolution_plan AS plan
    WHERE plan.owner_user_id = memory.current_actor_user_id()
      AND plan.resolution_id = p_resolution_id
      AND plan.predicate_registry_version = 'memory_predicate_registry_v5_1'
  ) THEN
    RAISE EXCEPTION 'owner-scoped V5.1 resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  RETURN QUERY SELECT * FROM memory.apply_entity_resolution_v5(
    p_request_id, p_resolution_id, p_review_id,
    p_apply_manifest_sha256
  );
END
$function$;

ALTER TABLE memory.entity_resolution_reconciliation_v5_1
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_reconciliation_v5_1(
  uuid, uuid, uuid, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.reconcile_entity_resolution_v5_1(
  uuid, uuid, uuid, uuid, text, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_review_v5_1(
  uuid, memory.entity_review_decision, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.review_entity_resolution_v5_1(
  uuid, uuid, memory.entity_review_decision, text, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_apply_v5_1(uuid, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_entity_resolution_v5_1(uuid, uuid, uuid, text)
  OWNER TO memory_v5_writer;

ALTER TABLE memory.entity_resolution_reconciliation_v5_1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entity_resolution_reconciliation_v5_1
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.entity_resolution_reconciliation_v5_1;
CREATE POLICY owner_isolation
  ON memory.entity_resolution_reconciliation_v5_1
  TO memory_v5_writer
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS entity_resolution_reconciliation_v5_1_append_only_guard
  ON memory.entity_resolution_reconciliation_v5_1;
CREATE TRIGGER entity_resolution_reconciliation_v5_1_append_only_guard
BEFORE UPDATE OR DELETE ON memory.entity_resolution_reconciliation_v5_1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

REVOKE ALL ON memory.entity_resolution_reconciliation_v5_1
  FROM PUBLIC, brains_app;
GRANT SELECT, INSERT ON memory.entity_resolution_reconciliation_v5_1
  TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_entity_resolution_reconciliation_v5_1(
  uuid, uuid, uuid, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.reconcile_entity_resolution_v5_1(
  uuid, uuid, uuid, uuid, text, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_entity_resolution_review_v5_1(
  uuid, memory.entity_review_decision, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.review_entity_resolution_v5_1(
  uuid, uuid, memory.entity_review_decision, text, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_entity_resolution_apply_v5_1(uuid, uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_entity_resolution_v5_1(
  uuid, uuid, uuid, text
) FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_reconciliation_v5_1(
  uuid, uuid, uuid, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.reconcile_entity_resolution_v5_1(
  uuid, uuid, uuid, uuid, text, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_review_v5_1(
  uuid, memory.entity_review_decision, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_entity_resolution_v5_1(
  uuid, uuid, memory.entity_review_decision, text, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_apply_v5_1(uuid, uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_entity_resolution_v5_1(
  uuid, uuid, uuid, text
) TO brains_app;

COMMIT;
