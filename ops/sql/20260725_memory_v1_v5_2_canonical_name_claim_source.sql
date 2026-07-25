BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.preflight_projection_source_v5_2(uuid)') IS NULL
     OR to_regprocedure(
       'memory.preflight_projection_entailment_source_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_projection_temporal_state_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.render_projection_claim_text_temporal_v5_2(uuid)'
     ) IS NULL
     OR to_regclass(
       'memory.entity_correction_target_reconciliation_v5_2'
     ) IS NULL THEN
    RAISE EXCEPTION
      'V5.2 canonical-name claim-source prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_source_v5_2(
  p_observation_id uuid
)
RETURNS TABLE(
  owner_user_id uuid,
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  evidence_status text,
  predicate_registry_version text,
  predicate text,
  polarity text,
  modality text,
  projection_class text,
  surface_policy text,
  sensitivity text,
  extraction_confidence text,
  subject_entity_id uuid,
  subject_entity_type text,
  subject_entity_status text,
  subject_canonical_name text,
  object_kind text,
  object_entity_id uuid,
  object_entity_type text,
  object_entity_status text,
  object_canonical_name text,
  object_literal jsonb,
  object_literal_sha256 text,
  project_scope jsonb,
  temporal jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  RETURN QUERY
  SELECT
    observation.owner_user_id,
    observation.observation_id,
    observation.observation_sha256,
    observation.evidence_id,
    evidence.content_sha256,
    evidence.status::text,
    CASE
      WHEN compatibility.canonical_name_correction
      THEN 'memory_predicate_registry_v5_2'
      ELSE observation.predicate_registry_version
    END,
    observation.predicate,
    observation.polarity::text,
    observation.modality::text,
    CASE
      WHEN compatibility.canonical_name_correction THEN 'direct_claim'
      ELSE observation.projection_class::text
    END,
    observation.surface_policy::text,
    observation.sensitivity::text,
    observation.extraction_confidence::text,
    binding.subject_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
    subject_entity.canonical_name,
    CASE
      WHEN binding.object_entity_id IS NULL THEN 'literal'
      ELSE 'entity'
    END,
    binding.object_entity_id,
    object_entity.entity_type,
    object_entity.status::text,
    object_entity.canonical_name,
    CASE
      WHEN compatibility.canonical_name_correction
      THEN jsonb_set(
        observation.object_literal,
        '{value}',
        to_jsonb(subject_entity.canonical_name),
        false
      )
      ELSE observation.object_literal
    END,
    CASE
      WHEN observation.object_literal IS NULL THEN NULL
      WHEN compatibility.canonical_name_correction THEN
        memory.v5_digest_text(memory.v5_canonical_json_text(jsonb_set(
          observation.object_literal,
          '{value}',
          to_jsonb(subject_entity.canonical_name),
          false
        )))
      ELSE memory.v5_digest_text(
        memory.v5_canonical_json_text(observation.object_literal)
      )
    END,
    CASE
      WHEN observation.project_scope->>'state'='resolved'
      THEN observation.project_scope || jsonb_build_object(
        'project_id', project.project_id::text
      )
      ELSE observation.project_scope
    END,
    jsonb_build_object(
      'semantic', temporal.semantic::text,
      'shape', temporal.shape::text,
      'basis', temporal.basis::text,
      'source_form', temporal.source_form::text,
      'certainty', temporal.certainty::text,
      'precision', temporal.precision::text,
      'normalized_sha256', temporal.normalized_sha256
    )
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  CROSS JOIN LATERAL (
    SELECT EXISTS (
      SELECT 1
      FROM memory.entity_correction_target_reconciliation_v5_2
        AS reconciliation
      WHERE reconciliation.owner_user_id=observation.owner_user_id
        AND reconciliation.observation_id=observation.observation_id
        AND reconciliation.successor_resolution_id=
          binding.subject_resolution_id
        AND reconciliation.target_entity_id=binding.subject_entity_id
        AND observation.predicate_registry_version=
          'memory_predicate_registry_v5'
        AND observation.predicate='identity.name_canonical'
        AND observation.modality='corrective'
        AND observation.polarity='affirmed'
        AND observation.projection_class='correction'
        AND observation.surface_policy='direct_or_relevant'
        AND observation.object_literal->>'kind'='literal'
        AND observation.object_literal->>'datatype'='text'
        AND lower(btrim(observation.object_literal->>'value'))=
          lower(btrim(subject_entity.canonical_name))
    ) AS canonical_name_correction
  ) AS compatibility
  JOIN memory.predicate_contract AS contract
    ON contract.predicate=observation.predicate
   AND contract.registry_version=CASE
     WHEN compatibility.canonical_name_correction
     THEN 'memory_predicate_registry_v5_2'
     ELSE observation.predicate_registry_version
   END
  LEFT JOIN memory.project_space AS project
    ON project.owner_user_id=observation.owner_user_id
   AND project.project_key=observation.project_scope->>'project_key'
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND (
      observation.predicate_registry_version=
        'memory_predicate_registry_v5_2'
      OR compatibility.canonical_name_correction
    )
    AND evidence.status='active'
    AND subject_entity.status='active'
    AND (
      binding.object_entity_id IS NULL
      OR object_entity.status='active'
    )
    AND contract.lifecycle='active'
    AND contract.extraction_allowed
    AND contract.contract->'modalities'?observation.modality::text
    AND contract.contract->'projection_classes'?(
      CASE
        WHEN compatibility.canonical_name_correction THEN 'direct_claim'
        ELSE observation.projection_class::text
      END
    )
    AND contract.contract->'surface_policies'?observation.surface_policy::text
    AND (
      observation.project_scope->>'state'<>'resolved'
      OR project.project_id IS NOT NULL
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete owner-scoped V5.2 projection source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.preflight_projection_entailment_source_v5_2(
  p_observation_id uuid
)
RETURNS TABLE(
  observation_id uuid,
  observation_ref text,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  source_spans jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  RETURN QUERY
  SELECT
    observation.observation_id,
    observation.observation_ref,
    observation.observation_sha256,
    observation.evidence_id,
    evidence.content_sha256,
    observation.source_spans
  FROM memory.observation AS observation
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  CROSS JOIN LATERAL (
    SELECT EXISTS (
      SELECT 1
      FROM memory.entity_correction_target_reconciliation_v5_2
        AS reconciliation
      WHERE reconciliation.owner_user_id=observation.owner_user_id
        AND reconciliation.observation_id=observation.observation_id
        AND reconciliation.successor_resolution_id=
          binding.subject_resolution_id
        AND reconciliation.target_entity_id=binding.subject_entity_id
        AND observation.predicate_registry_version=
          'memory_predicate_registry_v5'
        AND observation.predicate='identity.name_canonical'
        AND observation.modality='corrective'
        AND observation.polarity='affirmed'
        AND observation.projection_class='correction'
        AND observation.surface_policy='direct_or_relevant'
        AND observation.object_literal->>'kind'='literal'
        AND observation.object_literal->>'datatype'='text'
        AND lower(btrim(observation.object_literal->>'value'))=
          lower(btrim(subject_entity.canonical_name))
    ) AS canonical_name_correction
  ) AS compatibility
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND (
      observation.predicate_registry_version=
        'memory_predicate_registry_v5_2'
      OR compatibility.canonical_name_correction
    )
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND evidence.content_sha256=memory.v5_digest_text(evidence.content)
    AND subject_entity.status='active'
    AND (
      binding.object_entity_id IS NULL
      OR object_entity.status='active'
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION
      'complete owner-scoped V5.2 entailment source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.preflight_reusable_observation_entailment_v5_2(
  p_observation_id uuid,
  p_source_spans jsonb
)
RETURNS TABLE(
  decision_id uuid,
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  policy_version text,
  decision text,
  reason_code text,
  authorization_manifest_sha256 text,
  assessor_type text,
  assessor_ref text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_observation_id IS NULL
     OR NOT memory.v5_source_spans_valid(p_source_spans) THEN
    RAISE EXCEPTION 'reusable entailment inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  RETURN QUERY
  SELECT
    entailment.decision_id,
    entailment.observation_id,
    entailment.observation_sha256,
    entailment.evidence_id,
    entailment.evidence_content_sha256,
    entailment.policy_version,
    entailment.decision::text,
    entailment.reason_code,
    entailment.authorization_manifest_sha256,
    entailment.assessor_type,
    entailment.assessor_ref
  FROM memory.observation_entailment_v5 AS entailment
  JOIN memory.observation AS observation
    ON observation.owner_user_id=entailment.owner_user_id
   AND observation.observation_id=entailment.observation_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  WHERE entailment.owner_user_id=actor
    AND entailment.observation_id=p_observation_id
    AND entailment.observation_sha256=observation.observation_sha256
    AND entailment.evidence_id=observation.evidence_id
    AND entailment.evidence_content_sha256=evidence.content_sha256
    AND evidence.status='active'
    AND evidence.content IS NOT NULL
    AND evidence.content_sha256=memory.v5_digest_text(evidence.content)
    AND entailment.policy_version='memory_v1_predicate_entailment_v5_1'
    AND entailment.decision='accepted'
    AND entailment.reason_code='predicate_entailment_v5_1_accepted'
    AND entailment.source_spans=p_source_spans
    AND memory.v5_source_spans_match_text(
      entailment.source_spans,
      evidence.content
    )
    AND memory.v5_source_spans_cover(
      entailment.source_spans,
      observation.source_spans
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION
      'exact reusable owner-scoped observation entailment not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.preflight_projection_temporal_state_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  source record;
  relation_value text;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_observation_id);
  SELECT CASE
    WHEN temporal.semantic <> 'state_validity' THEN 'not_applicable'
    WHEN temporal.basis = 'instant'
      AND temporal.instant_range IS NOT NULL
      AND NOT upper_inf(temporal.instant_range) THEN 'historical'
    WHEN temporal.basis = 'calendar'
      AND temporal.calendar_range IS NOT NULL
      AND NOT upper_inf(temporal.calendar_range) THEN 'historical'
    WHEN temporal.basis = 'instant'
      AND temporal.instant_range IS NOT NULL
      AND upper_inf(temporal.instant_range) THEN 'current'
    WHEN temporal.basis = 'calendar'
      AND temporal.calendar_range IS NOT NULL
      AND upper_inf(temporal.calendar_range) THEN 'current'
    WHEN temporal.instant_at IS NOT NULL THEN 'point'
    ELSE 'unknown'
  END
  INTO STRICT relation_value
  FROM memory.observation_temporal AS temporal
  WHERE temporal.owner_user_id=source.owner_user_id
    AND temporal.observation_id=source.observation_id;
  RETURN relation_value;
EXCEPTION
  WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
    RAISE EXCEPTION 'exact owner-scoped temporal projection source not found'
      USING ERRCODE='P0002';
END
$function$;

CREATE OR REPLACE FUNCTION
memory.render_projection_claim_text_temporal_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  source record;
  subject_label text;
  object_label text;
  state_relation text;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_observation_id);
  state_relation := memory.preflight_projection_temporal_state_v5_2(
    p_observation_id
  );
  subject_label := CASE source.subject_entity_type
    WHEN 'self' THEN 'The user'
    ELSE source.subject_canonical_name
  END;
  IF source.predicate='identity.name_canonical'
     AND source.object_kind='literal'
     AND source.modality='corrective'
     AND source.projection_class='direct_claim'
     AND source.object_literal->>'value'=subject_label THEN
    RETURN CASE
      WHEN subject_label='The user' THEN 'The user''s '
      WHEN right(subject_label,1)='s' THEN subject_label||''' '
      ELSE subject_label||'''s '
    END || 'canonical name is ' ||
      (source.object_literal->>'value') || '.';
  END IF;
  IF source.predicate <> 'occupation.works_as'
     OR state_relation <> 'historical' THEN
    RETURN memory.render_projection_claim_text_v5_2(p_observation_id);
  END IF;
  object_label := CASE source.object_entity_type
    WHEN 'self' THEN 'The user'
    ELSE source.object_canonical_name
  END;
  IF source.object_kind <> 'entity'
     OR object_label IS NULL
     OR btrim(object_label)='' THEN
    RAISE EXCEPTION
      'historical occupation projection requires an entity object';
  END IF;
  RETURN subject_label || CASE
    WHEN source.polarity='negated' THEN
      ' did not formerly work as ' || object_label || '.'
    WHEN source.modality='uncertain' THEN
      ' may formerly have worked as ' || object_label || '.'
    ELSE
      ' formerly worked as ' || object_label || '.'
  END;
END
$function$;

DO $compatibility$
DECLARE
  definition text;
  old_block text := $old$
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
          AND NOT (
            link.stance = 'context'
            AND item.predicate = 'stance.reported'
            AND EXISTS (
              SELECT 1
              FROM memory.projection_plan AS plan
              WHERE plan.owner_user_id = item.owner_user_id
                AND plan.plan_id = item.plan_id
                AND plan.projector =
                      'memory_v1_deterministic_projection_v5_2'
                AND plan.projector_version = 'stance_reconciliation_v1'
            )
          )
        )
      )
$old$;
  new_block text := $new$
      -- canonical_name_claim_source_v5_2_compat
      AND NOT (
        (
          binding.subject_entity_id = item.subject_entity_id
          AND observation.predicate = item.predicate
          AND observation.polarity = item.polarity
          AND observation.modality = item.modality
          AND observation.projection_class::text = expected_class
          AND observation.surface_policy = expected_surface
          AND (
            (
              item.object_kind = 'entity'
              AND binding.object_entity_id IS NOT DISTINCT FROM
                item.object_entity_id
            )
            OR (
              item.object_kind = 'literal'
              AND (
                memory.v5_digest_text(memory.v5_canonical_json_text(
                  observation.object_literal
                )) = item.object_literal_sha256
                OR (
                  link.stance = 'context'
                  AND item.predicate = 'stance.reported'
                  AND EXISTS (
                    SELECT 1
                    FROM memory.projection_plan AS plan
                    WHERE plan.owner_user_id = item.owner_user_id
                      AND plan.plan_id = item.plan_id
                      AND plan.projector =
                            'memory_v1_deterministic_projection_v5_2'
                      AND plan.projector_version =
                            'stance_reconciliation_v1'
                  )
                )
              )
            )
          )
        )
        OR (
          observation.predicate_registry_version =
            'memory_predicate_registry_v5'
          AND observation.predicate = 'identity.name_canonical'
          AND item.predicate = 'identity.name_canonical'
          AND observation.polarity = 'affirmed'
          AND item.polarity = observation.polarity
          AND observation.modality = 'corrective'
          AND item.modality = observation.modality
          AND observation.projection_class = 'correction'
          AND expected_class = 'direct_claim'
          AND observation.surface_policy = 'direct_or_relevant'
          AND expected_surface = observation.surface_policy
          AND binding.subject_entity_id = item.subject_entity_id
          AND binding.object_entity_id IS NULL
          AND item.object_kind = 'literal'
          AND EXISTS (
            SELECT 1
            FROM memory.entity_correction_target_reconciliation_v5_2
              AS reconciliation
            JOIN memory.entity AS canonical_entity
              ON canonical_entity.owner_user_id =
                   reconciliation.owner_user_id
             AND canonical_entity.entity_id =
                   reconciliation.target_entity_id
            WHERE reconciliation.owner_user_id =
                    observation.owner_user_id
              AND reconciliation.observation_id =
                    observation.observation_id
              AND reconciliation.successor_resolution_id =
                    binding.subject_resolution_id
              AND reconciliation.target_entity_id =
                    binding.subject_entity_id
              AND canonical_entity.status = 'active'
              AND lower(btrim(observation.object_literal->>'value')) =
                    lower(btrim(canonical_entity.canonical_name))
              AND item.object_literal_sha256 =
                    memory.v5_digest_text(memory.v5_canonical_json_text(
                      jsonb_set(
                        observation.object_literal,
                        '{value}',
                        to_jsonb(canonical_entity.canonical_name),
                        false
                      )
                    ))
          )
        )
      )
$new$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.guard_projection_item_complete_v5()'::regprocedure
  ) INTO STRICT definition;
  IF strpos(
       definition,
       'canonical_name_claim_source_v5_2_compat'
     ) > 0 THEN
    RETURN;
  END IF;
  IF strpos(definition, old_block) = 0 THEN
    RAISE EXCEPTION
      'projection item integrity definition is outside the expected baseline';
  END IF;
  definition := replace(definition, old_block, new_block);
  IF strpos(
       definition,
       'canonical_name_claim_source_v5_2_compat'
     ) = 0
     OR strpos(definition, old_block) > 0 THEN
    RAISE EXCEPTION
      'projection item integrity compatibility replacement failed';
  END IF;
  EXECUTE definition;
END
$compatibility$;

ALTER FUNCTION memory.preflight_projection_source_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_entailment_source_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_reusable_observation_entailment_v5_2(
  uuid, jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_temporal_state_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.render_projection_claim_text_temporal_v5_2(uuid)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_projection_entailment_source_v5_2(uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_reusable_observation_entailment_v5_2(uuid, jsonb)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.preflight_projection_temporal_state_v5_2(uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION
  memory.render_projection_claim_text_temporal_v5_2(uuid)
  FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_projection_entailment_source_v5_2(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_reusable_observation_entailment_v5_2(uuid, jsonb)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_projection_temporal_state_v5_2(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.render_projection_claim_text_temporal_v5_2(uuid)
  TO brains_app;

COMMIT;
