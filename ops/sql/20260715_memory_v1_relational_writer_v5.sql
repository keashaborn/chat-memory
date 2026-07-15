BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 relational writer V5 migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.entity_mention') IS NULL
     OR to_regclass('memory.entity_resolution_plan') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL THEN
    RAISE EXCEPTION 'memory V1 relational staging V5 is required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto')
     OR NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'unaccent') THEN
    RAISE EXCEPTION 'pgcrypto and unaccent extensions are required';
  END IF;
END
$block$;

DO $block$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_v5_writer') THEN
    CREATE ROLE memory_v5_writer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$block$;

ALTER ROLE memory_v5_writer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS memory.relational_stage_batch (
  owner_user_id uuid NOT NULL,
  batch_id uuid NOT NULL DEFAULT gen_random_uuid(),
  evidence_id uuid NOT NULL,
  extraction_packet_text text NOT NULL,
  resolution_packet_text text NOT NULL,
  extraction_packet_sha256 text NOT NULL,
  resolution_packet_sha256 text NOT NULL,
  stage_manifest_sha256 text NOT NULL,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  mention_count integer NOT NULL,
  resolution_count integer NOT NULL,
  candidate_count integer NOT NULL,
  observation_count integer NOT NULL,
  temporal_count integer NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, batch_id),
  UNIQUE (
    owner_user_id, evidence_id,
    extraction_packet_sha256, resolution_packet_sha256
  ),
  UNIQUE (owner_user_id, stage_manifest_sha256),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id) ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(extraction_packet_sha256)),
  CHECK (memory.v5_sha256_valid(resolution_packet_sha256)),
  CHECK (memory.v5_sha256_valid(stage_manifest_sha256)),
  CHECK (octet_length(extraction_packet_text) <= 1048576),
  CHECK (octet_length(resolution_packet_text) <= 1048576),
  CHECK (
    encode(public.digest(convert_to(extraction_packet_text, 'UTF8'), 'sha256'), 'hex')
      = extraction_packet_sha256
  ),
  CHECK (
    encode(public.digest(convert_to(resolution_packet_text, 'UTF8'), 'sha256'), 'hex')
      = resolution_packet_sha256
  ),
  CHECK (btrim(extractor) <> '' AND length(extractor) <= 120),
  CHECK (btrim(extractor_version) <> '' AND length(extractor_version) <= 120),
  CHECK (mention_count BETWEEN 0 AND 24),
  CHECK (resolution_count = mention_count),
  CHECK (candidate_count BETWEEN 0 AND 480),
  CHECK (observation_count BETWEEN 0 AND 32),
  CHECK (temporal_count = observation_count),
  CHECK (jsonb_typeof(result) = 'object'),
  CHECK (pg_column_size(result) <= 65536)
);

CREATE TABLE IF NOT EXISTS memory.relational_operation_request (
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  operation text NOT NULL,
  target_key text NOT NULL,
  manifest_sha256 text NOT NULL,
  outcome text NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, request_id),
  UNIQUE (owner_user_id, operation, manifest_sha256),
  CHECK (operation IN ('stage_packet', 'review_resolution', 'apply_resolution')),
  CHECK (btrim(target_key) <> '' AND length(target_key) <= 500),
  CHECK (memory.v5_sha256_valid(manifest_sha256)),
  CHECK (outcome = 'applied'),
  CHECK (jsonb_typeof(result) = 'object'),
  CHECK (pg_column_size(result) <= 65536)
);

CREATE INDEX IF NOT EXISTS relational_stage_batch_owner_evidence_idx
  ON memory.relational_stage_batch(owner_user_id, evidence_id, created_at DESC);
CREATE INDEX IF NOT EXISTS relational_operation_request_owner_time_idx
  ON memory.relational_operation_request(owner_user_id, created_at DESC);

ALTER TABLE memory.relational_stage_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.relational_stage_batch FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.relational_stage_batch;
CREATE POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

ALTER TABLE memory.relational_operation_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.relational_operation_request FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.relational_operation_request;
CREATE POLICY owner_isolation ON memory.relational_operation_request
  TO memory_v5_writer
  USING (owner_user_id = (SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS relational_stage_batch_append_only_guard
  ON memory.relational_stage_batch;
CREATE TRIGGER relational_stage_batch_append_only_guard
BEFORE UPDATE OR DELETE ON memory.relational_stage_batch
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

DROP TRIGGER IF EXISTS relational_operation_request_append_only_guard
  ON memory.relational_operation_request;
CREATE TRIGGER relational_operation_request_append_only_guard
BEFORE UPDATE OR DELETE ON memory.relational_operation_request
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

DO $rls$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'entity_mention',
    'entity_resolution_plan',
    'entity_resolution_candidate',
    'entity_resolution_review',
    'entity_resolution_apply',
    'entity_alias_observation',
    'observation',
    'observation_temporal',
    'observation_entity_binding',
    'claim_observation',
    'candidate_observation'
  ]
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I TO memory_v5_writer '
      'USING (owner_user_id = (SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id = (SELECT memory.current_actor_user_id()))',
      table_name
    );
  END LOOP;
END
$rls$;

CREATE OR REPLACE FUNCTION memory.v5_jsonb_exact_keys(
  value jsonb,
  expected_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT jsonb_typeof(value) = 'object'
    AND value ?& expected_keys
    AND (SELECT count(*) FROM jsonb_object_keys(value)) = cardinality(expected_keys)
    AND cardinality(expected_keys) = (
      SELECT count(DISTINCT key_name) FROM unnest(expected_keys) AS keys(key_name)
    )
$function$;

CREATE OR REPLACE FUNCTION memory.v5_digest_text(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT encode(public.digest(convert_to(value, 'UTF8'), 'sha256'), 'hex')
$function$;

CREATE OR REPLACE FUNCTION memory.normalize_entity_name_v5(value text)
RETURNS text
LANGUAGE sql
STABLE
STRICT
SECURITY INVOKER
SET search_path = ''
AS $function$
  SELECT btrim(lower(regexp_replace(
    public.unaccent(value), '[^[:alnum:]]+', ' ', 'g'
  )))
$function$;

CREATE OR REPLACE FUNCTION memory.require_v5_writer_context()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'memory V5 writer functions require a brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'memory V5 writer functions require an actor user ID'
      USING ERRCODE = '42501';
  END IF;
  RETURN actor;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_relational_packet_v5(
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
    RAISE EXCEPTION 'V5 packets exceed the 1 MiB limit'
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
    RAISE EXCEPTION 'V5 packet top-level fields mismatch'
      USING ERRCODE = '23514';
  END IF;
  IF extraction_packet->>'contract_version' <> 'memory_v1_relational_extraction_v5'
     OR extraction_packet->>'predicate_registry_version' <> 'memory_predicate_registry_v5'
     OR resolution_packet->>'contract_version' <> 'memory_v1_entity_resolution_review_v5'
     OR resolution_packet->>'predicate_registry_version' <> 'memory_predicate_registry_v5'
     OR resolution_packet->>'entity_normalization_version' <> 'memory_entity_normalization_v5'
     OR NOT memory.v5_sha256_valid(resolution_packet->>'packet_sha256') THEN
    RAISE EXCEPTION 'V5 packet version or resolver packet hash mismatch'
      USING ERRCODE = '23514';
  END IF;
  IF jsonb_typeof(extraction_packet->'entity_mentions') <> 'array'
     OR jsonb_array_length(extraction_packet->'entity_mentions') > 24
     OR jsonb_typeof(extraction_packet->'observations') <> 'array'
     OR jsonb_array_length(extraction_packet->'observations') > 32
     OR jsonb_typeof(resolution_packet->'resolutions') <> 'array'
     OR jsonb_array_length(resolution_packet->'resolutions') > 24 THEN
    RAISE EXCEPTION 'V5 packet arrays exceed contract limits'
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
    RAISE EXCEPTION 'source envelopes do not match the V5 contract'
      USING ERRCODE = '23514';
  END IF;

  SELECT * INTO evidence_row
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = actor
    AND evidence.evidence_id = p_evidence_id
  FOR KEY SHARE;
  IF NOT FOUND OR evidence_row.status <> 'active'
     OR evidence_row.source_system <> 'public.chat_log'
     OR evidence_row.external_id <> extraction_source->>'source_external_id'
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
    'memory_v1_stage_packet_v5', actor::text, p_evidence_id::text,
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
      'memory_predicate_registry_v5', 'memory_entity_normalization_v5',
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
      observation->>'predicate', 'memory_predicate_registry_v5',
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

CREATE OR REPLACE FUNCTION memory.preflight_entity_resolution_review_v5(
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
DECLARE
  actor uuid;
  plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF btrim(COALESCE(p_reason, '')) = '' OR length(p_reason) > 2000 THEN
    RAISE EXCEPTION 'review reason is required and limited to 2000 characters'
      USING ERRCODE = '22023';
  END IF;
  SELECT * INTO plan FROM memory.entity_resolution_plan AS staged_plan
  WHERE staged_plan.owner_user_id = actor
    AND staged_plan.resolution_id = p_resolution_id;
  IF NOT FOUND OR plan.decision_state <> 'manual_review_required' THEN
    RAISE EXCEPTION 'owner-scoped manual-review resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT * INTO STRICT mention FROM memory.entity_mention AS staged_mention
  WHERE staged_mention.owner_user_id = actor
    AND staged_mention.mention_id = plan.mention_id;
  SELECT * INTO STRICT evidence FROM memory.evidence AS source_evidence
  WHERE source_evidence.owner_user_id = actor
    AND source_evidence.evidence_id = plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'review requires active evidence'
      USING ERRCODE = '23514';
  END IF;
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_review_resolution_v5', actor::text, p_resolution_id::text,
    evidence.content_sha256, mention.mention_sha256,
    plan.candidate_set_sha256, plan.decision_sha256,
    plan.resolver, plan.resolver_version, plan.action::text,
    COALESCE(plan.selected_entity_id::text, plan.proposed_entity::text, ''),
    p_decision::text, btrim(p_reason)
  ));
  RETURN QUERY SELECT
    plan.resolution_id, plan.action, plan.decision_state,
    mention.mention_sha256, plan.candidate_set_sha256,
    plan.decision_sha256, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_entity_resolution_v5(
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
DECLARE
  actor uuid;
  preflight record;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_review memory.entity_resolution_review%ROWTYPE;
  review_id_value uuid := gen_random_uuid();
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_request_id IS NULL THEN
    RAISE EXCEPTION 'request_id is required'
      USING ERRCODE = '22004';
  END IF;
  PERFORM 1 FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id
  FOR UPDATE;
  PERFORM 1 FROM memory.evidence AS evidence
  JOIN memory.entity_resolution_plan AS plan
    ON plan.owner_user_id = evidence.owner_user_id
   AND plan.evidence_id = evidence.evidence_id
  WHERE plan.owner_user_id = actor AND plan.resolution_id = p_resolution_id
  FOR UPDATE OF evidence;
  SELECT * INTO preflight
  FROM memory.preflight_entity_resolution_review_v5(
    p_resolution_id, p_decision, p_reason
  );
  IF p_authorization_manifest_sha256 <> preflight.authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'review authorization manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor AND request.request_id = p_request_id;
  IF FOUND THEN
    IF existing_request.operation <> 'review_resolution'
       OR existing_request.manifest_sha256 <> p_authorization_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'review_id')::uuid,
      'replayed', existing_request.result;
    RETURN;
  END IF;
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor
    AND request.operation = 'review_resolution'
    AND request.manifest_sha256 = p_authorization_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT
      (existing_request.result->>'review_id')::uuid,
      'replayed', existing_request.result;
    RETURN;
  END IF;
  SELECT * INTO existing_review
  FROM memory.entity_resolution_review AS review
  WHERE review.owner_user_id = actor
    AND review.resolution_id = p_resolution_id
    AND review.authorization_manifest_sha256 = p_authorization_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT existing_review.review_id, 'replayed', jsonb_build_object(
      'review_id', existing_review.review_id,
      'resolution_id', p_resolution_id,
      'decision', existing_review.decision
    );
    RETURN;
  END IF;
  INSERT INTO memory.entity_resolution_review(
    review_id, owner_user_id, resolution_id, decision,
    expected_mention_sha256, expected_candidate_set_sha256,
    expected_decision_sha256, reviewer_type, reviewer_ref,
    reason, authorization_manifest_sha256
  ) VALUES (
    review_id_value, actor, p_resolution_id, p_decision,
    preflight.mention_sha256, preflight.candidate_set_sha256,
    preflight.decision_sha256, 'user', actor::text,
    btrim(p_reason), p_authorization_manifest_sha256
  );
  result_value := jsonb_build_object(
    'review_id', review_id_value,
    'resolution_id', p_resolution_id,
    'decision', p_decision
  );
  INSERT INTO memory.relational_operation_request(
    request_id, owner_user_id, operation, target_key,
    manifest_sha256, outcome, result, invoked_by_session
  ) VALUES (
    p_request_id, actor, 'review_resolution', p_resolution_id::text,
    p_authorization_manifest_sha256, 'applied', result_value, session_user
  );
  RETURN QUERY SELECT review_id_value, 'applied', result_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_entity_resolution_apply_v5(
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
DECLARE
  actor uuid;
  plan memory.entity_resolution_plan%ROWTYPE;
  mention memory.entity_mention%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  entity_row memory.entity%ROWTYPE;
  review memory.entity_resolution_review%ROWTYPE;
  entity_state text;
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT * INTO plan FROM memory.entity_resolution_plan AS staged_plan
  WHERE staged_plan.owner_user_id = actor
    AND staged_plan.resolution_id = p_resolution_id;
  IF NOT FOUND OR plan.decision_state IN ('deferred', 'rejected') THEN
    RAISE EXCEPTION 'owner-scoped applicable resolution not found'
      USING ERRCODE = 'P0002';
  END IF;
  SELECT * INTO STRICT mention FROM memory.entity_mention AS staged_mention
  WHERE staged_mention.owner_user_id = actor
    AND staged_mention.mention_id = plan.mention_id;
  SELECT * INTO STRICT evidence FROM memory.evidence AS source_evidence
  WHERE source_evidence.owner_user_id = actor
    AND source_evidence.evidence_id = plan.evidence_id;
  IF evidence.status <> 'active' THEN
    RAISE EXCEPTION 'resolution apply requires active evidence'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.entity_resolution_apply AS applied
    JOIN memory.entity_resolution_plan AS other_plan
      ON other_plan.owner_user_id = applied.owner_user_id
     AND other_plan.resolution_id = applied.resolution_id
    WHERE other_plan.owner_user_id = actor
      AND other_plan.mention_id = plan.mention_id
      AND other_plan.resolution_id <> plan.resolution_id
  ) THEN
    RAISE EXCEPTION 'mention already has a different applied resolution'
      USING ERRCODE = '23505';
  END IF;
  IF plan.decision_state = 'manual_review_required' THEN
    IF p_review_id IS NULL THEN
      RAISE EXCEPTION 'manual resolution requires review_id'
        USING ERRCODE = '23514';
    END IF;
    SELECT * INTO review FROM memory.entity_resolution_review AS staged_review
    WHERE staged_review.owner_user_id = actor
      AND staged_review.resolution_id = p_resolution_id
      AND staged_review.review_id = p_review_id;
    IF NOT FOUND OR review.decision <> 'approved'
       OR review.review_id IS DISTINCT FROM (
         SELECT latest.review_id
         FROM memory.entity_resolution_review AS latest
         WHERE latest.owner_user_id = actor
           AND latest.resolution_id = p_resolution_id
         ORDER BY latest.created_at DESC, latest.review_id DESC
         LIMIT 1
       ) THEN
      RAISE EXCEPTION 'latest resolution review is not the supplied approval'
        USING ERRCODE = '23514';
    END IF;
  ELSIF p_review_id IS NOT NULL THEN
    RAISE EXCEPTION 'auto-link resolution cannot carry review_id'
      USING ERRCODE = '23514';
  END IF;
  IF plan.action = 'link_existing' THEN
    SELECT * INTO entity_row FROM memory.entity AS selected_entity
    WHERE selected_entity.owner_user_id = actor
      AND selected_entity.entity_id = plan.selected_entity_id;
    IF NOT FOUND OR entity_row.status <> 'active' THEN
      RAISE EXCEPTION 'selected owner entity is not active'
        USING ERRCODE = '23514';
    END IF;
    entity_state := concat_ws('|',
      entity_row.entity_id::text, entity_row.entity_key, entity_row.entity_type,
      entity_row.canonical_name, entity_row.normalized_name,
      entity_row.status::text, entity_row.updated_at::text
    );
  ELSIF plan.action = 'create_new' THEN
    IF plan.proposed_entity->>'identity_state' <> 'named'
       OR plan.proposed_entity->>'canonical_name' IS NULL THEN
      RAISE EXCEPTION 'role-only and anonymous entity creation is not supported yet'
        USING ERRCODE = '0A000';
    END IF;
    entity_state := plan.proposed_entity::text;
  ELSE
    RAISE EXCEPTION 'resolution action is not applicable'
      USING ERRCODE = '23514';
  END IF;
  entity_state := memory.v5_digest_text(entity_state);
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_apply_resolution_v5', actor::text, p_resolution_id::text,
    evidence.content_sha256, mention.mention_sha256,
    plan.candidate_set_sha256, plan.decision_sha256,
    plan.action::text, plan.decision_state::text,
    COALESCE(p_review_id::text, ''),
    COALESCE(review.authorization_manifest_sha256, ''), entity_state
  ));
  RETURN QUERY SELECT
    plan.resolution_id, plan.action, plan.decision_state,
    p_review_id, plan.selected_entity_id, entity_state, manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_entity_resolution_v5(
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
  entity_row memory.entity%ROWTYPE;
  existing_request memory.relational_operation_request%ROWTYPE;
  existing_apply memory.entity_resolution_apply%ROWTYPE;
  normalized_entity_name text;
  entity_id_value uuid;
  alias_hash text;
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
    RAISE EXCEPTION 'request_id is required'
      USING ERRCODE = '22004';
  END IF;
  PERFORM 1 FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id
  FOR UPDATE;
  SELECT * INTO plan FROM memory.entity_resolution_plan
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id;
  SELECT * INTO mention FROM memory.entity_mention
  WHERE owner_user_id = actor AND mention_id = plan.mention_id;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text || '|mention|' || plan.mention_id::text,
    0
  ));
  PERFORM 1 FROM memory.evidence
  WHERE owner_user_id = actor AND evidence_id = plan.evidence_id
  FOR UPDATE;
  IF p_review_id IS NOT NULL THEN
    PERFORM 1 FROM memory.entity_resolution_review
    WHERE owner_user_id = actor
      AND resolution_id = p_resolution_id
      AND review_id = p_review_id
    FOR UPDATE;
  END IF;
  SELECT * INTO preflight
  FROM memory.preflight_entity_resolution_apply_v5(
    p_resolution_id, p_review_id
  );
  IF p_apply_manifest_sha256 <> preflight.apply_manifest_sha256 THEN
    RAISE EXCEPTION 'resolution apply manifest mismatch'
      USING ERRCODE = '23514';
  END IF;
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor AND request.request_id = p_request_id;
  IF FOUND THEN
    IF existing_request.operation <> 'apply_resolution'
       OR existing_request.manifest_sha256 <> p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'request_id replay payload mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      (existing_request.result->>'applied_entity_id')::uuid,
      'replayed', 0, existing_request.result;
    RETURN;
  END IF;
  SELECT * INTO existing_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id = actor
    AND request.operation = 'apply_resolution'
    AND request.manifest_sha256 = p_apply_manifest_sha256;
  IF FOUND THEN
    RETURN QUERY SELECT
      (existing_request.result->>'applied_entity_id')::uuid,
      'replayed', 0, existing_request.result;
    RETURN;
  END IF;
  SELECT * INTO existing_apply FROM memory.entity_resolution_apply
  WHERE owner_user_id = actor AND resolution_id = p_resolution_id;
  IF FOUND THEN
    IF existing_apply.apply_manifest_sha256 <> p_apply_manifest_sha256 THEN
      RAISE EXCEPTION 'applied resolution manifest mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT existing_apply.applied_entity_id, 'replayed', 0,
      jsonb_build_object(
        'resolution_id', p_resolution_id,
        'applied_entity_id', existing_apply.applied_entity_id,
        'bindings_created', 0
      );
    RETURN;
  END IF;

  IF plan.action = 'link_existing' THEN
    SELECT * INTO entity_row FROM memory.entity
    WHERE owner_user_id = actor AND entity_id = plan.selected_entity_id
    FOR UPDATE;
    IF plan.decision_state = 'auto_link_eligible' THEN
      IF mention.entity_type = 'self' THEN
        IF entity_row.entity_type <> 'self' OR entity_row.entity_key <> 'self' THEN
          RAISE EXCEPTION 'trusted self binding target mismatch'
            USING ERRCODE = '23514';
        END IF;
      ELSE
        IF mention.mention_kind <> 'named' THEN
          RAISE EXCEPTION 'only named non-self mentions may auto-link'
            USING ERRCODE = '23514';
        END IF;
        normalized_entity_name := memory.normalize_entity_name_v5(mention.name_text);
        IF entity_row.entity_type <> mention.entity_type
           OR entity_row.normalized_name <> normalized_entity_name
           OR (
             SELECT count(*) FROM memory.entity AS same_name
             WHERE same_name.owner_user_id = actor
               AND same_name.status = 'active'
               AND same_name.entity_type = mention.entity_type
               AND same_name.normalized_name = normalized_entity_name
           ) <> 1 THEN
          RAISE EXCEPTION 'auto-link owner-local entity set drifted or is ambiguous'
            USING ERRCODE = '23514';
        END IF;
      END IF;
    END IF;
    entity_id_value := entity_row.entity_id;
  ELSE
    normalized_entity_name := memory.normalize_entity_name_v5(
      plan.proposed_entity->>'canonical_name'
    );
    PERFORM pg_advisory_xact_lock(hashtextextended(
      actor::text || '|' || (plan.proposed_entity->>'entity_type')
        || '|' || normalized_entity_name,
      0
    ));
    IF EXISTS (
      SELECT 1 FROM memory.entity AS same_name
      WHERE same_name.owner_user_id = actor
        AND same_name.status = 'active'
        AND same_name.entity_type = plan.proposed_entity->>'entity_type'
        AND same_name.normalized_name = normalized_entity_name
    ) THEN
      RAISE EXCEPTION 'named entity creation conflicts with an active owner entity'
        USING ERRCODE = '23514';
    END IF;
    entity_id_value := gen_random_uuid();
    INSERT INTO memory.entity(
      entity_id, owner_user_id, entity_key, entity_type,
      canonical_name, normalized_name, metadata
    ) VALUES (
      entity_id_value, actor, 'ent_' || replace(gen_random_uuid()::text, '-', ''),
      plan.proposed_entity->>'entity_type',
      plan.proposed_entity->>'canonical_name', normalized_entity_name,
      jsonb_build_object(
        'origin', 'memory_v1_relational_v5',
        'identity_state', 'named',
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

  IF mention.mention_kind = 'named' THEN
    alias_hash := memory.v5_digest_text(concat_ws('|',
      mention.evidence_id::text, mention.mention_sha256,
      entity_id_value::text, mention.name_text,
      memory.normalize_entity_name_v5(mention.name_text),
      'memory_entity_normalization_v5'
    ));
    INSERT INTO memory.entity_alias_observation(
      owner_user_id, evidence_id, mention_id, resolution_id, entity_id,
      alias_text, normalized_alias, alias_type, source_spans,
      normalization_version, alias_sha256
    ) VALUES (
      actor, mention.evidence_id, mention.mention_id,
      p_resolution_id, entity_id_value,
      mention.name_text, memory.normalize_entity_name_v5(mention.name_text),
      'observed_name', mention.source_spans,
      'memory_entity_normalization_v5', alias_hash
    );
  END IF;

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
      COALESCE(object_resolution::text, ''), COALESCE(object_entity::text, '')
    ));
    INSERT INTO memory.observation_entity_binding(
      owner_user_id, observation_id,
      subject_resolution_id, subject_entity_id,
      object_resolution_id, object_entity_id, binding_manifest_sha256
    ) VALUES (
      actor, observation_row.observation_id,
      subject_resolution, subject_entity,
      object_resolution, object_entity, binding_hash
    );
    binding_count := binding_count + 1;
  END LOOP;

  result_value := jsonb_build_object(
    'resolution_id', p_resolution_id,
    'applied_entity_id', entity_id_value,
    'bindings_created', binding_count
  );
  INSERT INTO memory.relational_operation_request(
    request_id, owner_user_id, operation, target_key,
    manifest_sha256, outcome, result, invoked_by_session
  ) VALUES (
    p_request_id, actor, 'apply_resolution', p_resolution_id::text,
    p_apply_manifest_sha256, 'applied', result_value, session_user
  );
  RETURN QUERY SELECT entity_id_value, 'applied', binding_count, result_value;
END
$function$;

ALTER FUNCTION memory.v5_jsonb_exact_keys(jsonb, text[]) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.v5_digest_text(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.normalize_entity_name_v5(text) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_writer_context() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_relational_packet_v5(
  uuid, uuid, text, text, text, text, text, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_review_v5(
  uuid, memory.entity_review_decision, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.review_entity_resolution_v5(
  uuid, uuid, memory.entity_review_decision, text, text
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_entity_resolution_apply_v5(uuid, uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.apply_entity_resolution_v5(uuid, uuid, uuid, text)
  OWNER TO memory_v5_writer;

REVOKE ALL ON memory.relational_stage_batch FROM PUBLIC, brains_app;
REVOKE ALL ON memory.relational_operation_request FROM PUBLIC, brains_app;

GRANT USAGE ON SCHEMA memory TO memory_v5_writer;
GRANT USAGE ON SCHEMA memory TO brains_app;
GRANT USAGE ON TYPE
  memory.entity_mention_kind,
  memory.entity_resolution_action,
  memory.entity_resolution_state,
  memory.entity_review_decision,
  memory.observation_polarity,
  memory.observation_modality,
  memory.observation_projection_class,
  memory.observation_surface_policy,
  memory.temporal_semantic,
  memory.temporal_shape,
  memory.temporal_basis,
  memory.temporal_source_form,
  memory.temporal_certainty,
  memory.temporal_precision,
  memory.evidence_kind,
  memory.record_status,
  memory.sensitivity_level
TO memory_v5_writer;

GRANT SELECT ON
  memory.predicate,
  memory.predicate_registry_version,
  memory.predicate_contract,
  memory.evidence,
  memory.entity,
  memory.entity_alias,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.relational_stage_batch,
  memory.relational_operation_request
TO memory_v5_writer;

GRANT INSERT ON
  memory.entity,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_candidate,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_temporal,
  memory.observation_entity_binding,
  memory.relational_stage_batch,
  memory.relational_operation_request
TO memory_v5_writer;

GRANT UPDATE ON
  memory.evidence,
  memory.entity,
  memory.entity_resolution_plan,
  memory.entity_resolution_review
TO memory_v5_writer;

GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb, integer)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_source_spans_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_project_scope_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_literal_object_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_proposed_entity_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_candidate_features_valid(jsonb)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_relative_offset_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.v5_recurrence_valid(jsonb) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_actor_active_evidence()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_plan() TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_observation_contract()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_review()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_resolution_apply()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_observation_binding()
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_append_only() TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.v5_jsonb_exact_keys(jsonb, text[])
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_digest_text(text) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.normalize_entity_name_v5(text)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.require_v5_writer_context()
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.stage_relational_packet_v5(
  uuid, uuid, text, text, text, text, text, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_entity_resolution_review_v5(
  uuid, memory.entity_review_decision, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.review_entity_resolution_v5(
  uuid, uuid, memory.entity_review_decision, text, text
) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.preflight_entity_resolution_apply_v5(uuid, uuid)
  FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.apply_entity_resolution_v5(uuid, uuid, uuid, text)
  FROM PUBLIC, brains_app;

GRANT EXECUTE ON FUNCTION memory.stage_relational_packet_v5(
  uuid, uuid, text, text, text, text, text, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_review_v5(
  uuid, memory.entity_review_decision, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.review_entity_resolution_v5(
  uuid, uuid, memory.entity_review_decision, text, text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_entity_resolution_apply_v5(uuid, uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_entity_resolution_v5(uuid, uuid, uuid, text)
  TO brains_app;

COMMIT;
