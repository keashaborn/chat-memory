BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR NOT has_function_privilege(
       'memory_v5_writer',
       'memory.require_v5_writer_context()',
       'EXECUTE'
     )
     OR to_regprocedure(
       'memory.preflight_entity_resolution_review_v5_2(uuid,memory.entity_review_decision,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_entity_resolution_v5_2(uuid,uuid,uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 correction-target reconciliation prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.entity_correction_target_reconciliation_v5_2 (
  owner_user_id uuid NOT NULL,
  reconciliation_id uuid NOT NULL DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  source_resolution_id uuid NOT NULL,
  successor_resolution_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  target_entity_id uuid NOT NULL,
  match_basis text NOT NULL,
  source_decision_sha256 text NOT NULL,
  mention_sha256 text NOT NULL,
  observation_sha256 text NOT NULL,
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
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, target_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  CHECK (match_basis = 'unique_owner_normalized_corrected_name'),
  CHECK (memory.v5_sha256_valid(source_decision_sha256)),
  CHECK (memory.v5_sha256_valid(mention_sha256)),
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (memory.v5_sha256_valid(target_state_sha256)),
  CHECK (memory.v5_sha256_valid(candidate_set_sha256)),
  CHECK (memory.v5_sha256_valid(successor_decision_sha256)),
  CHECK (memory.v5_sha256_valid(reconciliation_manifest_sha256)),
  CHECK (btrim(reason) <> '' AND length(reason) <= 500)
);

CREATE INDEX IF NOT EXISTS entity_correction_target_reconciliation_v5_2_target_idx
  ON memory.entity_correction_target_reconciliation_v5_2(
    owner_user_id, target_entity_id, created_at DESC
  );

CREATE OR REPLACE FUNCTION memory.preflight_correction_target_reconciliation_v5_2(
  p_source_resolution_id uuid,
  p_successor_resolution_id uuid,
  p_target_entity_id uuid,
  p_reason text
)
RETURNS TABLE(
  source_resolution_id uuid,
  successor_resolution_id uuid,
  observation_id uuid,
  target_entity_id uuid,
  source_action memory.entity_resolution_action,
  source_decision_state memory.entity_resolution_state,
  match_basis text,
  mention_sha256 text,
  observation_sha256 text,
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
  correction memory.observation%ROWTYPE;
  correction_observation_id uuid;
  target memory.entity%ROWTYPE;
  correction_count integer;
  matching_target_count integer;
  corrected_name text;
  normalized_corrected_name text;
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
     OR source_plan.predicate_registry_version NOT IN (
       'memory_predicate_registry_v5',
       'memory_predicate_registry_v5_1',
       'memory_predicate_registry_v5_2'
     )
     OR source_plan.entity_normalization_version <> 'memory_entity_normalization_v5'
     OR source_plan.action <> 'defer'
     OR source_plan.decision_state <> 'deferred'
     OR NOT source_plan.review_reason_codes ? 'correction_target_resolution_required' THEN
    RAISE EXCEPTION 'owner-scoped deferred correction-target plan not found'
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
    RAISE EXCEPTION 'correction-target mention already has an applied resolution'
      USING ERRCODE = '23505';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.entity_resolution_plan AS successor
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
  IF mention.mention_kind NOT IN ('anonymous', 'role_only')
     OR mention.entity_type = 'self'
     OR mention.relationship_role <> 'pet:corrected_name_subject' THEN
    RAISE EXCEPTION 'correction-target mention contract mismatch'
      USING ERRCODE = '23514';
  END IF;

  SELECT * INTO STRICT evidence
  FROM memory.evidence AS source_evidence
  WHERE source_evidence.owner_user_id = actor
    AND source_evidence.evidence_id = source_plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'correction-target reconciliation requires active evidence'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*),
         (array_agg(observation.observation_id
                    ORDER BY observation.observation_id))[1]
  INTO correction_count, correction_observation_id
  FROM memory.observation AS observation
  WHERE observation.owner_user_id = actor
    AND observation.evidence_id = source_plan.evidence_id
    AND observation.subject_mention_id = source_plan.mention_id
    AND observation.object_mention_id IS NULL
    AND observation.predicate = 'identity.name_canonical'
    AND observation.modality = 'corrective'
    AND observation.polarity = 'affirmed'
    AND observation.object_literal->>'kind' = 'literal'
    AND observation.object_literal->>'datatype' = 'text'
    AND btrim(COALESCE(observation.object_literal->>'value', '')) <> '';
  IF correction_count <> 1 THEN
    RAISE EXCEPTION 'exactly one governed canonical-name correction is required'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT correction
  FROM memory.observation AS observation
  WHERE observation.owner_user_id = actor
    AND observation.observation_id = correction_observation_id;

  corrected_name := btrim(correction.object_literal->>'value');
  normalized_corrected_name := memory.normalize_entity_name_v5(corrected_name);
  SELECT * INTO target
  FROM memory.entity AS owner_entity
  WHERE owner_entity.owner_user_id = actor
    AND owner_entity.entity_id = p_target_entity_id;
  IF NOT FOUND
     OR target.status <> 'active'
     OR target.entity_type <> mention.entity_type
     OR target.normalized_name <> normalized_corrected_name THEN
    RAISE EXCEPTION 'target must be the active same-type owner entity with the corrected name'
      USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO matching_target_count
  FROM memory.entity AS same_name
  WHERE same_name.owner_user_id = actor
    AND same_name.status = 'active'
    AND same_name.entity_type = mention.entity_type
    AND same_name.normalized_name = normalized_corrected_name;
  IF matching_target_count <> 1 THEN
    RAISE EXCEPTION 'corrected owner-local entity name is absent or ambiguous'
      USING ERRCODE = '23514';
  END IF;

  target_state := memory.v5_digest_text(concat_ws('|',
    target.entity_id::text, target.entity_key, target.entity_type,
    target.canonical_name, target.normalized_name,
    target.status::text, target.updated_at::text,
    correction.observation_id::text, correction.observation_sha256,
    corrected_name, normalized_corrected_name, matching_target_count::text
  ));
  candidate_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_correction_target_candidate_v5_2', actor::text,
    source_plan.resolution_id::text, p_successor_resolution_id::text,
    p_target_entity_id::text, mention.mention_sha256,
    correction.observation_sha256, normalized_corrected_name,
    target_state, 'unique_owner_normalized_corrected_name'
  ));
  decision_hash := memory.v5_digest_text(concat_ws('|',
    'memory_v1_correction_target_decision_v5_2', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    p_successor_resolution_id::text, p_target_entity_id::text,
    candidate_hash, 'link_existing', 'manual_review_required'
  ));
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_correction_target_reconciliation_v5_2', actor::text,
    source_plan.resolution_id::text, source_plan.decision_sha256,
    evidence.content_sha256, mention.mention_sha256,
    correction.observation_sha256, corrected_name,
    p_successor_resolution_id::text, p_target_entity_id::text,
    target_state, candidate_hash, decision_hash,
    'unique_owner_normalized_corrected_name', btrim(p_reason)
  ));
  RETURN QUERY SELECT
    source_plan.resolution_id, p_successor_resolution_id,
    correction.observation_id, target.entity_id,
    source_plan.action, source_plan.decision_state,
    'unique_owner_normalized_corrected_name'::text,
    mention.mention_sha256, correction.observation_sha256,
    target_state, candidate_hash, decision_hash, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.reconcile_correction_target_v5_2(
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
  observation_id uuid,
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
  existing memory.entity_correction_target_reconciliation_v5_2%ROWTYPE;
  reconciliation_id_value uuid := gen_random_uuid();
  features jsonb;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL THEN
    RAISE EXCEPTION 'request_id is required' USING ERRCODE = '22004';
  END IF;
  SELECT * INTO existing
  FROM memory.entity_correction_target_reconciliation_v5_2 AS prior
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
      'observation_id', existing.observation_id,
      'target_entity_id', existing.target_entity_id
    );
    RETURN QUERY SELECT existing.reconciliation_id,
      existing.successor_resolution_id, existing.observation_id,
      existing.target_entity_id, 'replayed'::text, result_value;
    RETURN;
  END IF;
  SELECT * INTO existing
  FROM memory.entity_correction_target_reconciliation_v5_2 AS prior
  WHERE prior.owner_user_id = actor
    AND prior.reconciliation_manifest_sha256 = p_reconciliation_manifest_sha256;
  IF FOUND THEN
    result_value := jsonb_build_object(
      'reconciliation_id', existing.reconciliation_id,
      'source_resolution_id', existing.source_resolution_id,
      'successor_resolution_id', existing.successor_resolution_id,
      'observation_id', existing.observation_id,
      'target_entity_id', existing.target_entity_id
    );
    RETURN QUERY SELECT existing.reconciliation_id,
      existing.successor_resolution_id, existing.observation_id,
      existing.target_entity_id, 'replayed'::text, result_value;
    RETURN;
  END IF;

  PERFORM 1 FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_source_resolution_id
  FOR UPDATE;
  SELECT * INTO source_plan
  FROM memory.entity_resolution_plan
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
  FROM memory.preflight_correction_target_reconciliation_v5_2(
    p_source_resolution_id, p_successor_resolution_id,
    p_target_entity_id, p_reason
  );
  IF p_reconciliation_manifest_sha256 <>
     preflight.reconciliation_manifest_sha256 THEN
    RAISE EXCEPTION 'correction-target reconciliation manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO STRICT mention
  FROM memory.entity_mention
  WHERE owner_user_id = actor AND mention_id = source_plan.mention_id;

  INSERT INTO memory.entity_resolution_plan(
    resolution_id, owner_user_id, evidence_id, mention_id,
    predicate_registry_version, entity_normalization_version,
    resolver, resolver_version, action, decision_state,
    selected_entity_id, proposed_entity, candidate_set_sha256,
    decision_sha256, review_reason_codes
  ) VALUES (
    p_successor_resolution_id, actor, source_plan.evidence_id,
    source_plan.mention_id, 'memory_predicate_registry_v5_2',
    'memory_entity_normalization_v5',
    'governed_correction_target_reconciliation',
    'memory_v1_correction_target_reconciliation_v5_2',
    'link_existing', 'manual_review_required', p_target_entity_id, NULL,
    preflight.candidate_set_sha256, preflight.successor_decision_sha256,
    '["unique_owner_normalized_corrected_name",
      "manual_correction_target_review_required"]'::jsonb
  );
  features := jsonb_build_object(
    'active_status', true,
    'entity_type_match', true,
    'exact_canonical_name', true,
    'exact_alias', EXISTS (
      SELECT 1
      FROM memory.entity_alias_observation AS alias
      WHERE alias.owner_user_id = actor
        AND alias.entity_id = p_target_entity_id
        AND alias.normalized_alias = (
          SELECT normalized_name
          FROM memory.entity
          WHERE owner_user_id = actor AND entity_id = p_target_entity_id
        )
    ),
    'relationship_role_supported', true,
    'source_local_coreference', true,
    'graph_neighbor_supported', EXISTS (
      SELECT 1
      FROM memory.claim AS governed_claim
      WHERE governed_claim.owner_user_id = actor
        AND governed_claim.status = 'supported'
        AND (
          governed_claim.subject_entity_id = p_target_entity_id
          OR governed_claim.object_entity_id = p_target_entity_id
        )
    ),
    'conflicting_attribute_count', 0,
    'same_name_candidate_count', 1
  );
  INSERT INTO memory.entity_resolution_candidate(
    owner_user_id, resolution_id, candidate_entity_id,
    ordinal, entity_type, features, exclusion_reasons
  ) VALUES (
    actor, p_successor_resolution_id, p_target_entity_id, 1,
    mention.entity_type, features, '[]'::jsonb
  );
  INSERT INTO memory.entity_correction_target_reconciliation_v5_2(
    owner_user_id, reconciliation_id, request_id,
    source_resolution_id, successor_resolution_id, observation_id,
    target_entity_id, match_basis, source_decision_sha256,
    mention_sha256, observation_sha256, target_state_sha256,
    candidate_set_sha256, successor_decision_sha256,
    reconciliation_manifest_sha256, reason
  ) VALUES (
    actor, reconciliation_id_value, p_request_id,
    p_source_resolution_id, p_successor_resolution_id,
    preflight.observation_id, p_target_entity_id,
    preflight.match_basis, source_plan.decision_sha256,
    preflight.mention_sha256, preflight.observation_sha256,
    preflight.target_state_sha256, preflight.candidate_set_sha256,
    preflight.successor_decision_sha256,
    p_reconciliation_manifest_sha256, btrim(p_reason)
  );
  result_value := jsonb_build_object(
    'reconciliation_id', reconciliation_id_value,
    'source_resolution_id', p_source_resolution_id,
    'successor_resolution_id', p_successor_resolution_id,
    'observation_id', preflight.observation_id,
    'target_entity_id', p_target_entity_id
  );
  RETURN QUERY SELECT reconciliation_id_value,
    p_successor_resolution_id, preflight.observation_id,
    p_target_entity_id, 'applied'::text, result_value;
END
$function$;

ALTER TABLE memory.entity_correction_target_reconciliation_v5_2
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_correction_target_reconciliation_v5_2(
  uuid, uuid, uuid, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.reconcile_correction_target_v5_2(
  uuid, uuid, uuid, uuid, text, text
) OWNER TO memory_v5_writer;

ALTER TABLE memory.entity_correction_target_reconciliation_v5_2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entity_correction_target_reconciliation_v5_2
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.entity_correction_target_reconciliation_v5_2;
CREATE POLICY owner_isolation
  ON memory.entity_correction_target_reconciliation_v5_2
  TO memory_v5_writer
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS entity_correction_target_reconciliation_v5_2_append_only_guard
  ON memory.entity_correction_target_reconciliation_v5_2;
CREATE TRIGGER entity_correction_target_reconciliation_v5_2_append_only_guard
BEFORE UPDATE OR DELETE ON memory.entity_correction_target_reconciliation_v5_2
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

REVOKE ALL ON memory.entity_correction_target_reconciliation_v5_2
  FROM PUBLIC, brains_app;
GRANT SELECT, INSERT ON memory.entity_correction_target_reconciliation_v5_2
  TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_correction_target_reconciliation_v5_2(
  uuid, uuid, uuid, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.reconcile_correction_target_v5_2(
  uuid, uuid, uuid, uuid, text, text
) FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_correction_target_reconciliation_v5_2(
  uuid, uuid, uuid, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.reconcile_correction_target_v5_2(
  uuid, uuid, uuid, uuid, text, text
) TO brains_app;

COMMIT;
