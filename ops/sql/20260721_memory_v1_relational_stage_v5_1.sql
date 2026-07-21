BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regclass('memory.evidence') IS NULL THEN
    RAISE EXCEPTION 'V5 stage source-ID compatibility prerequisites are absent';
  END IF;
END
$preflight$;

DO $registry_constraint$
DECLARE
  constraint_definition text;
BEGIN
  SELECT pg_get_constraintdef(constraint_row.oid)
  INTO constraint_definition
  FROM pg_constraint AS constraint_row
  WHERE constraint_row.conrelid='memory.entity_resolution_plan'::regclass
    AND constraint_row.conname=
      'entity_resolution_plan_predicate_registry_version_check';
  IF constraint_definition IS NULL THEN
    RAISE EXCEPTION 'entity resolution registry constraint is absent';
  END IF;
  IF position('memory_predicate_registry_v5_1' IN constraint_definition)=0 THEN
    IF position('memory_predicate_registry_v5' IN constraint_definition)=0 THEN
      RAISE EXCEPTION 'entity resolution registry constraint is unknown';
    END IF;
    ALTER TABLE memory.entity_resolution_plan
      DROP CONSTRAINT entity_resolution_plan_predicate_registry_version_check;
    ALTER TABLE memory.entity_resolution_plan
      ADD CONSTRAINT entity_resolution_plan_predicate_registry_version_check
      CHECK (predicate_registry_version IN (
        'memory_predicate_registry_v5',
        'memory_predicate_registry_v5_1'
      ));
  END IF;
END
$registry_constraint$;

CREATE OR REPLACE FUNCTION memory.stage_relational_packet_v5_1(
  p_request_id uuid,
  p_evidence_id uuid,
  p_extractor text,
  p_extractor_version text,
  p_extraction_packet_text text,
  p_resolution_packet_text text,
  p_extraction_packet_sha256 text,
  p_resolution_packet_sha256 text
)
RETURNS TABLE(
  batch_id uuid,
  outcome text,
  mentions_inserted integer,
  resolutions_inserted integer,
  candidates_inserted integer,
  observations_inserted integer,
  temporals_inserted integer,
  result jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  extraction_packet jsonb;
  resolution_packet jsonb;
  extraction_source jsonb;
  resolution_source jsonb;
  evidence_row memory.evidence%ROWTYPE;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_batch memory.relational_stage_batch%ROWTYPE;
  stage_manifest text;
  mention jsonb;
  resolution jsonb;
  candidate jsonb;
  observation jsonb;
  temporal jsonb;
  object_value jsonb;
  project_scope jsonb;
  mention_id_value uuid;
  subject_mention_id_value uuid;
  object_mention_id_value uuid;
  resolution_id_value uuid;
  observation_id_value uuid;
  batch_id_value uuid := gen_random_uuid();
  mention_count_value integer := 0;
  resolution_count_value integer := 0;
  candidate_count_value integer := 0;
  candidate_ordinal_value smallint := 0;
  observation_count_value integer := 0;
  temporal_count_value integer := 0;
  result_value jsonb;
  mention_map jsonb := '{}'::jsonb;
  resolution_map jsonb := '{}'::jsonb;
  observation_map jsonb := '{}'::jsonb;
  selected_entity_id_value uuid;
  actual_extraction_sha text;
  actual_resolution_sha text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL OR p_evidence_id IS NULL THEN
    RAISE EXCEPTION 'request_id and evidence_id are required'
      USING ERRCODE = '22004';
  END IF;
  IF btrim(COALESCE(p_extractor, '')) = ''
     OR length(p_extractor) > 120
     OR btrim(COALESCE(p_extractor_version, '')) = ''
     OR length(p_extractor_version) > 120 THEN
    RAISE EXCEPTION 'invalid extractor identity'
      USING ERRCODE = '22023';
  END IF;
  IF octet_length(p_extraction_packet_text) > 1048576
     OR octet_length(p_resolution_packet_text) > 1048576 THEN
    RAISE EXCEPTION 'V5.1 packets exceed the 1 MiB limit'
      USING ERRCODE = '54000';
  END IF;
  IF NOT memory.v5_sha256_valid(p_extraction_packet_sha256)
     OR NOT memory.v5_sha256_valid(p_resolution_packet_sha256) THEN
    RAISE EXCEPTION 'packet hashes must be lowercase SHA-256 hex'
      USING ERRCODE = '22023';
  END IF;
  actual_extraction_sha := memory.v5_digest_text(p_extraction_packet_text);
  actual_resolution_sha := memory.v5_digest_text(p_resolution_packet_text);
  IF actual_extraction_sha <> p_extraction_packet_sha256
     OR actual_resolution_sha <> p_resolution_packet_sha256 THEN
    RAISE EXCEPTION 'raw packet hash mismatch'
      USING ERRCODE = '23514';
  END IF;

  extraction_packet := p_extraction_packet_text::jsonb;
  resolution_packet := p_resolution_packet_text::jsonb;
  IF NOT memory.v5_jsonb_exact_keys(extraction_packet, ARRAY[
    'contract_version', 'source_envelope', 'predicate_registry_version',
    'entity_mentions', 'observations', 'comparison_hints',
    'deferrals', 'packet_findings'
  ]) OR NOT memory.v5_jsonb_exact_keys(resolution_packet, ARRAY[
    'contract_version', 'source_envelope', 'predicate_registry_version',
    'entity_normalization_version', 'resolver', 'resolver_version',
    'resolutions', 'packet_sha256'
  ]) THEN
    RAISE EXCEPTION 'V5.1 packet top-level fields mismatch'
      USING ERRCODE = '23514';
  END IF;
  IF extraction_packet->>'contract_version' <> 'memory_v1_relational_extraction_v5_1'
     OR extraction_packet->>'predicate_registry_version' <> 'memory_predicate_registry_v5_1'
     OR resolution_packet->>'contract_version' <> 'memory_v1_entity_resolution_review_v5_1'
     OR resolution_packet->>'predicate_registry_version' <> 'memory_predicate_registry_v5_1'
     OR resolution_packet->>'entity_normalization_version' <> 'memory_entity_normalization_v5'
     OR NOT memory.v5_sha256_valid(resolution_packet->>'packet_sha256') THEN
    RAISE EXCEPTION 'V5.1 packet version or resolver packet hash mismatch'
      USING ERRCODE = '23514';
  END IF;
  IF jsonb_typeof(extraction_packet->'entity_mentions') <> 'array'
     OR jsonb_array_length(extraction_packet->'entity_mentions') > 24
     OR jsonb_typeof(extraction_packet->'observations') <> 'array'
     OR jsonb_array_length(extraction_packet->'observations') > 32
     OR jsonb_typeof(resolution_packet->'resolutions') <> 'array'
     OR jsonb_array_length(resolution_packet->'resolutions') > 24 THEN
    RAISE EXCEPTION 'V5.1 packet arrays exceed contract limits'
      USING ERRCODE = '23514';
  END IF;

  extraction_source := extraction_packet->'source_envelope';
  resolution_source := resolution_packet->'source_envelope';
  IF NOT memory.v5_jsonb_exact_keys(extraction_source, ARRAY[
    'job_id', 'source_system', 'source_external_id',
    'source_sha256', 'source_recorded_at'
  ]) OR extraction_source IS DISTINCT FROM resolution_source
     OR extraction_source->>'source_system' <> 'public.chat_log'
     OR NOT memory.v5_sha256_valid(extraction_source->>'source_sha256') THEN
    RAISE EXCEPTION 'source envelopes do not match the V5.1 contract'
      USING ERRCODE = '23514';
  END IF;

  SELECT * INTO evidence_row
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = actor
    AND evidence.evidence_id = p_evidence_id
  FOR KEY SHARE;
  IF NOT FOUND OR evidence_row.status <> 'active'
     OR evidence_row.source_system <> 'public.chat_log'
     OR extraction_source->>'source_external_id' IS DISTINCT FROM (CASE
       WHEN evidence_row.external_id ~*
         '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         THEN lower(evidence_row.external_id)
       ELSE evidence_row.evidence_id::text
     END)
     OR evidence_row.content_sha256 IS DISTINCT FROM extraction_source->>'source_sha256' THEN
    RAISE EXCEPTION 'active owner-scoped evidence does not match source envelope'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(extraction_packet->'entity_mentions') AS rows(value)
    GROUP BY value->>'entity_ref' HAVING count(*) <> 1
  ) OR EXISTS (
    SELECT 1
    FROM jsonb_array_elements(resolution_packet->'resolutions') AS rows(value)
    GROUP BY value->>'entity_ref' HAVING count(*) <> 1
  ) OR jsonb_array_length(extraction_packet->'entity_mentions')
       <> jsonb_array_length(resolution_packet->'resolutions')
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(extraction_packet->'entity_mentions') AS mentions(value)
       WHERE NOT EXISTS (
         SELECT 1
         FROM jsonb_array_elements(resolution_packet->'resolutions') AS resolutions(value)
         WHERE resolutions.value->>'entity_ref' = mentions.value->>'entity_ref'
       )
     ) THEN
    RAISE EXCEPTION 'each mention requires exactly one owner-resolver decision'
      USING ERRCODE = '23514';
  END IF;

  stage_manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_stage_packet_v5_1', actor::text, p_evidence_id::text,
    p_extraction_packet_sha256, p_resolution_packet_sha256,
    p_extractor, p_extractor_version
  ));

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor
    AND request.request_id = p_request_id;
  IF FOUND THEN
    IF existing_request.operation <> 'stage_packet'
       OR existing_request.manifest_sha256 <> stage_manifest THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'batch_id')::uuid,
      'replayed', 0, 0, 0, 0, 0, existing_request.result;
    RETURN;
  END IF;

  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor
    AND request.operation = 'stage_packet'
    AND request.manifest_sha256 = stage_manifest;
  IF FOUND THEN
    RETURN QUERY SELECT
      (existing_request.result->>'batch_id')::uuid,
      'replayed', 0, 0, 0, 0, 0, existing_request.result;
    RETURN;
  END IF;

  SELECT * INTO existing_batch
  FROM memory.relational_stage_batch AS batch
  WHERE batch.owner_user_id = actor
    AND batch.evidence_id = p_evidence_id
    AND batch.extraction_packet_sha256 = p_extraction_packet_sha256
    AND batch.resolution_packet_sha256 = p_resolution_packet_sha256;
  IF FOUND THEN
    IF existing_batch.stage_manifest_sha256 <> stage_manifest THEN
      RAISE EXCEPTION 'packet replay extractor identity mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      existing_batch.batch_id, 'replayed', 0, 0, 0, 0, 0, existing_batch.result;
    RETURN;
  END IF;

  FOR mention IN
    SELECT value FROM jsonb_array_elements(extraction_packet->'entity_mentions')
  LOOP
    IF NOT memory.v5_jsonb_exact_keys(mention, ARRAY[
      'entity_ref', 'entity_type', 'mention_kind', 'name_text',
      'relationship_role', 'source_spans', 'extraction_confidence', 'reason_codes'
    ]) THEN
      RAISE EXCEPTION 'entity mention fields mismatch'
        USING ERRCODE = '23514';
    END IF;
    SELECT value INTO STRICT resolution
    FROM jsonb_array_elements(resolution_packet->'resolutions')
    WHERE value->>'entity_ref' = mention->>'entity_ref';
    IF NOT memory.v5_jsonb_exact_keys(resolution, ARRAY[
      'entity_ref', 'mention_sha256', 'action', 'decision_state',
      'selected_entity_id', 'proposed_entity', 'candidate_set_sha256',
      'candidate_set', 'review_reason_codes', 'decision_sha256'
    ]) THEN
      RAISE EXCEPTION 'resolution fields mismatch'
        USING ERRCODE = '23514';
    END IF;
    mention_id_value := gen_random_uuid();
    INSERT INTO memory.entity_mention(
      mention_id, owner_user_id, evidence_id, packet_sha256,
      entity_ref, entity_type, mention_kind, name_text, relationship_role,
      source_spans, extraction_confidence, reason_codes,
      extractor, extractor_version, mention_sha256
    ) VALUES (
      mention_id_value, actor, p_evidence_id, p_extraction_packet_sha256,
      mention->>'entity_ref', mention->>'entity_type',
      (mention->>'mention_kind')::memory.entity_mention_kind,
      mention->>'name_text', mention->>'relationship_role',
      mention->'source_spans', (mention->>'extraction_confidence')::numeric,
      mention->'reason_codes', p_extractor, p_extractor_version,
      resolution->>'mention_sha256'
    );
    mention_map := mention_map || jsonb_build_object(
      mention->>'entity_ref', mention_id_value::text
    );
    mention_count_value := mention_count_value + 1;
  END LOOP;

  FOR resolution IN
    SELECT value FROM jsonb_array_elements(resolution_packet->'resolutions')
  LOOP
    SELECT mention_row.mention_id INTO STRICT mention_id_value
    FROM memory.entity_mention AS mention_row
    WHERE mention_row.owner_user_id = actor
      AND mention_row.evidence_id = p_evidence_id
      AND mention_row.packet_sha256 = p_extraction_packet_sha256
      AND mention_row.entity_ref = resolution->>'entity_ref';
    selected_entity_id_value := NULLIF(resolution->>'selected_entity_id', '')::uuid;
    resolution_id_value := gen_random_uuid();
    INSERT INTO memory.entity_resolution_plan(
      resolution_id, owner_user_id, evidence_id, mention_id,
      predicate_registry_version, entity_normalization_version,
      resolver, resolver_version, action, decision_state,
      selected_entity_id, proposed_entity, candidate_set_sha256,
      decision_sha256, review_reason_codes
    ) VALUES (
      resolution_id_value, actor, p_evidence_id, mention_id_value,
      'memory_predicate_registry_v5_1', 'memory_entity_normalization_v5',
      resolution_packet->>'resolver', resolution_packet->>'resolver_version',
      (resolution->>'action')::memory.entity_resolution_action,
      (resolution->>'decision_state')::memory.entity_resolution_state,
      selected_entity_id_value,
      CASE WHEN resolution->'proposed_entity' = 'null'::jsonb
        THEN NULL ELSE resolution->'proposed_entity' END,
      resolution->>'candidate_set_sha256', resolution->>'decision_sha256',
      resolution->'review_reason_codes'
    );
    resolution_map := resolution_map || jsonb_build_object(
      resolution->>'entity_ref', resolution_id_value::text
    );
    resolution_count_value := resolution_count_value + 1;

    IF jsonb_typeof(resolution->'candidate_set') <> 'array'
       OR jsonb_array_length(resolution->'candidate_set') > 20 THEN
      RAISE EXCEPTION 'candidate_set exceeds the contract limit'
        USING ERRCODE = '23514';
    END IF;
    candidate_ordinal_value := 0;
    FOR candidate IN
      SELECT value FROM jsonb_array_elements(resolution->'candidate_set')
    LOOP
      IF NOT memory.v5_jsonb_exact_keys(candidate, ARRAY[
        'entity_id', 'entity_type', 'features', 'exclusion_reasons'
      ]) THEN
        RAISE EXCEPTION 'resolution candidate fields mismatch'
          USING ERRCODE = '23514';
      END IF;
      candidate_ordinal_value := candidate_ordinal_value + 1;
      INSERT INTO memory.entity_resolution_candidate(
        owner_user_id, resolution_id, candidate_entity_id,
        ordinal, entity_type, features, exclusion_reasons
      ) VALUES (
        actor, resolution_id_value, (candidate->>'entity_id')::uuid,
        candidate_ordinal_value,
        candidate->>'entity_type', candidate->'features',
        candidate->'exclusion_reasons'
      );
      candidate_count_value := candidate_count_value + 1;
    END LOOP;
  END LOOP;

  FOR observation IN
    SELECT value FROM jsonb_array_elements(extraction_packet->'observations')
  LOOP
    IF NOT memory.v5_jsonb_exact_keys(observation, ARRAY[
      'observation_ref', 'subject_entity_ref', 'predicate',
      'predicate_registry_status', 'object', 'polarity', 'modality',
      'projection_class', 'surface_policy', 'temporal', 'project_scope',
      'sensitivity', 'extraction_confidence', 'source_spans', 'reason_codes'
    ]) OR observation->>'predicate_registry_status' <> 'governed' THEN
      RAISE EXCEPTION 'observation fields mismatch'
        USING ERRCODE = '23514';
    END IF;
    SELECT mention_row.mention_id INTO STRICT subject_mention_id_value
    FROM memory.entity_mention AS mention_row
    WHERE mention_row.owner_user_id = actor
      AND mention_row.evidence_id = p_evidence_id
      AND mention_row.packet_sha256 = p_extraction_packet_sha256
      AND mention_row.entity_ref = observation->>'subject_entity_ref';
    object_value := observation->'object';
    object_mention_id_value := NULL;
    IF object_value->>'kind' = 'entity' THEN
      IF NOT memory.v5_jsonb_exact_keys(object_value, ARRAY['kind', 'entity_ref']) THEN
        RAISE EXCEPTION 'entity object fields mismatch'
          USING ERRCODE = '23514';
      END IF;
      SELECT mention_row.mention_id INTO STRICT object_mention_id_value
      FROM memory.entity_mention AS mention_row
      WHERE mention_row.owner_user_id = actor
        AND mention_row.evidence_id = p_evidence_id
        AND mention_row.packet_sha256 = p_extraction_packet_sha256
        AND mention_row.entity_ref = object_value->>'entity_ref';
    ELSIF object_value->>'kind' = 'literal' THEN
      IF NOT memory.v5_literal_object_valid(object_value) THEN
        RAISE EXCEPTION 'literal object fields mismatch'
          USING ERRCODE = '23514';
      END IF;
    ELSE
      RAISE EXCEPTION 'unknown observation object kind'
        USING ERRCODE = '23514';
    END IF;
    project_scope := observation->'project_scope';
    temporal := observation->'temporal';
    IF NOT memory.v5_jsonb_exact_keys(temporal, ARRAY[
      'semantic', 'shape', 'basis', 'source_form', 'certainty', 'precision',
      'instant', 'calendar_range', 'instant_range', 'relative_offset',
      'recurrence', 'anchored_to_source_time',
      'normalization_policy_version', 'reason_codes'
    ]) THEN
      RAISE EXCEPTION 'temporal fields mismatch'
        USING ERRCODE = '23514';
    END IF;

    observation_id_value := gen_random_uuid();
    INSERT INTO memory.observation(
      observation_id, owner_user_id, evidence_id, observation_ref,
      subject_mention_id, predicate, predicate_registry_version,
      object_mention_id, object_literal, polarity, modality,
      projection_class, surface_policy, project_scope, sensitivity,
      extraction_confidence, source_spans, reason_codes,
      extractor, extractor_version, packet_sha256, observation_sha256
    ) VALUES (
      observation_id_value, actor, p_evidence_id,
      observation->>'observation_ref', subject_mention_id_value,
      observation->>'predicate', 'memory_predicate_registry_v5_1',
      object_mention_id_value,
      CASE WHEN object_value->>'kind' = 'literal' THEN object_value ELSE NULL END,
      (observation->>'polarity')::memory.observation_polarity,
      (observation->>'modality')::memory.observation_modality,
      (observation->>'projection_class')::memory.observation_projection_class,
      (observation->>'surface_policy')::memory.observation_surface_policy,
      project_scope, (observation->>'sensitivity')::memory.sensitivity_level,
      (observation->>'extraction_confidence')::numeric,
      observation->'source_spans', observation->'reason_codes',
      p_extractor, p_extractor_version, p_extraction_packet_sha256,
      memory.v5_digest_text(observation::text)
    );

    INSERT INTO memory.observation_temporal(
      owner_user_id, observation_id, semantic, shape, basis, source_form,
      certainty, precision, instant_at, calendar_range, instant_range,
      relative_offset, recurrence, anchored_to_source_time,
      normalization_policy_version, reason_codes, normalized_sha256
    ) VALUES (
      actor, observation_id_value,
      (temporal->>'semantic')::memory.temporal_semantic,
      (temporal->>'shape')::memory.temporal_shape,
      (temporal->>'basis')::memory.temporal_basis,
      (temporal->>'source_form')::memory.temporal_source_form,
      (temporal->>'certainty')::memory.temporal_certainty,
      (temporal->>'precision')::memory.temporal_precision,
      NULLIF(temporal->>'instant', '')::timestamptz,
      CASE WHEN temporal->'calendar_range' = 'null'::jsonb THEN NULL
        ELSE daterange(
          NULLIF(temporal->'calendar_range'->>'lower', '')::date,
          NULLIF(temporal->'calendar_range'->>'upper', '')::date,
          '[)'
        ) END,
      CASE WHEN temporal->'instant_range' = 'null'::jsonb THEN NULL
        ELSE tstzrange(
          NULLIF(temporal->'instant_range'->>'lower', '')::timestamptz,
          NULLIF(temporal->'instant_range'->>'upper', '')::timestamptz,
          '[)'
        ) END,
      CASE WHEN temporal->'relative_offset' = 'null'::jsonb
        THEN NULL ELSE temporal->'relative_offset' END,
      CASE WHEN temporal->'recurrence' = 'null'::jsonb
        THEN NULL ELSE temporal->'recurrence' END,
      (temporal->>'anchored_to_source_time')::boolean,
      temporal->>'normalization_policy_version', temporal->'reason_codes',
      memory.v5_digest_text(temporal::text)
    );
    observation_map := observation_map || jsonb_build_object(
      observation->>'observation_ref', observation_id_value::text
    );
    observation_count_value := observation_count_value + 1;
    temporal_count_value := temporal_count_value + 1;
  END LOOP;

  result_value := jsonb_build_object(
    'batch_id', batch_id_value,
    'mention_ids', mention_map,
    'resolution_ids', resolution_map,
    'observation_ids', observation_map,
    'mention_count', mention_count_value,
    'resolution_count', resolution_count_value,
    'candidate_count', candidate_count_value,
    'observation_count', observation_count_value,
    'temporal_count', temporal_count_value
  );
  INSERT INTO memory.relational_stage_batch(
    batch_id, owner_user_id, evidence_id,
    extraction_packet_text, resolution_packet_text,
    extraction_packet_sha256, resolution_packet_sha256,
    stage_manifest_sha256, extractor, extractor_version,
    mention_count, resolution_count, candidate_count,
    observation_count, temporal_count, result, invoked_by_session
  ) VALUES (
    batch_id_value, actor, p_evidence_id,
    p_extraction_packet_text, p_resolution_packet_text,
    p_extraction_packet_sha256, p_resolution_packet_sha256,
    stage_manifest, p_extractor, p_extractor_version,
    mention_count_value, resolution_count_value, candidate_count_value,
    observation_count_value, temporal_count_value, result_value, session_user
  );
  INSERT INTO memory.relational_operation_request(
    request_id, owner_user_id, operation, target_key,
    manifest_sha256, outcome, result, invoked_by_session
  ) VALUES (
    p_request_id, actor, 'stage_packet', p_evidence_id::text,
    stage_manifest, 'applied', result_value, session_user
  );
  RETURN QUERY SELECT
    batch_id_value, 'applied', mention_count_value, resolution_count_value,
    candidate_count_value, observation_count_value, temporal_count_value,
    result_value;
END
$function$;


ALTER FUNCTION memory.stage_relational_packet_v5_1(
  uuid,uuid,text,text,text,text,text,text
) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.stage_relational_packet_v5_1(
  uuid,uuid,text,text,text,text,text,text
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_relational_packet_v5_1(
  uuid,uuid,text,text,text,text,text,text
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.preflight_relational_stage_bundle_v5_1(
  p_evidence_id uuid,
  p_source_external_id text,
  p_source_sha256 text,
  p_source_recorded_at timestamptz
)
RETURNS TABLE(
  evidence_id uuid,
  verified boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched_id uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_evidence_id IS NULL
     OR btrim(COALESCE(p_source_external_id,''))=''
     OR NOT memory.v5_sha256_valid(p_source_sha256)
     OR p_source_recorded_at IS NULL THEN
    RAISE EXCEPTION 'V5.1 stage preflight inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT evidence.evidence_id INTO matched_id
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_evidence_id
    AND evidence.status='active'
    AND evidence.source_system='public.chat_log'
    AND p_source_external_id=(CASE
      WHEN evidence.external_id ~*
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN lower(evidence.external_id)
      ELSE evidence.evidence_id::text
    END)
    AND evidence.content_sha256=p_source_sha256
    AND evidence.recorded_at=p_source_recorded_at;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped stage evidence is unavailable'
      USING ERRCODE='P0002';
  END IF;
  RETURN QUERY SELECT matched_id,true;
END
$function$;

ALTER FUNCTION memory.preflight_relational_stage_bundle_v5_1(
  uuid,text,text,timestamptz
) OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_relational_stage_bundle_v5_1(
  uuid,text,text,timestamptz
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_relational_stage_bundle_v5_1(
  uuid,text,text,timestamptz
) TO brains_app;

COMMIT;
