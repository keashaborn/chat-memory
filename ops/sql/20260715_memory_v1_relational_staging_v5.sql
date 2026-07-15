BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 relational staging V5 migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.entity') IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.candidate') IS NULL THEN
    RAISE EXCEPTION 'memory V1 foundation is required';
  END IF;
END
$block$;

DO $block$ BEGIN
  CREATE TYPE memory.entity_mention_kind AS ENUM (
    'self_reference', 'named', 'role_only', 'anonymous'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.entity_resolution_action AS ENUM (
    'link_existing', 'create_new', 'defer', 'reject'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.entity_resolution_state AS ENUM (
    'auto_link_eligible', 'manual_review_required', 'deferred', 'rejected'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.entity_review_decision AS ENUM ('approved', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.observation_polarity AS ENUM ('affirmed', 'negated');
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.observation_modality AS ENUM (
    'asserted', 'negated', 'uncertain', 'corrective',
    'proposed', 'planned', 'endorsed', 'reported_observation'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.observation_projection_class AS ENUM (
    'direct_claim', 'supportive_context', 'correction', 'life_preference',
    'response_preference', 'project_knowledge', 'never_surface'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.observation_surface_policy AS ENUM (
    'direct_or_relevant', 'mention_when_directly_relevant',
    'explicit_recall_only', 'normalization_only', 'exact_project_scope_only',
    'relevant_recommendation_or_explicit_recall',
    'zero_token_control_only', 'never'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_semantic AS ENUM (
    'occurrence', 'state_validity', 'planned_time', 'observation_time', 'none'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_shape AS ENUM (
    'none', 'instant', 'bounded_interval', 'open_interval', 'recurring'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_basis AS ENUM (
    'none', 'instant', 'calendar', 'relative', 'recurring'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_source_form AS ENUM (
    'none', 'absolute', 'partial_absolute', 'relative', 'implicit_source_time'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_certainty AS ENUM (
    'exact', 'approximate', 'bounded', 'unknown'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

DO $block$ BEGIN
  CREATE TYPE memory.temporal_precision AS ENUM (
    'exact', 'minute', 'day', 'month', 'year', 'unknown'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $block$;

CREATE OR REPLACE FUNCTION memory.v5_sha256_valid(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
  SELECT value ~ '^[0-9a-f]{64}$'
$function$;

CREATE OR REPLACE FUNCTION memory.v5_reason_codes_valid(value jsonb, max_items integer)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  item jsonb;
  item_text text;
BEGIN
  IF jsonb_typeof(value) <> 'array'
     OR jsonb_array_length(value) > max_items THEN
    RETURN false;
  END IF;
  FOR item IN SELECT element FROM jsonb_array_elements(value) AS rows(element)
  LOOP
    IF jsonb_typeof(item) <> 'string' THEN
      RETURN false;
    END IF;
    item_text := item #>> '{}';
    IF item_text !~ '^[a-z][a-z0-9_]{1,99}$' THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN (
    SELECT count(*) = count(DISTINCT element)
    FROM jsonb_array_elements_text(value) AS rows(element)
  );
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_source_spans_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  item jsonb;
  key_count integer;
BEGIN
  IF jsonb_typeof(value) <> 'array'
     OR jsonb_array_length(value) NOT BETWEEN 1 AND 8 THEN
    RETURN false;
  END IF;
  FOR item IN SELECT element FROM jsonb_array_elements(value) AS rows(element)
  LOOP
    SELECT count(*) INTO key_count FROM jsonb_object_keys(item);
    IF jsonb_typeof(item) <> 'object'
       OR key_count <> 3
       OR NOT (item ?& ARRAY['start', 'end', 'span_sha256'])
       OR jsonb_typeof(item->'start') <> 'number'
       OR jsonb_typeof(item->'end') <> 'number'
       OR (item->>'start')::numeric <> trunc((item->>'start')::numeric)
       OR (item->>'end')::numeric <> trunc((item->>'end')::numeric)
       OR (item->>'start')::integer < 0
       OR (item->>'end')::integer <= (item->>'start')::integer
       OR NOT memory.v5_sha256_valid(item->>'span_sha256') THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_project_scope_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
  state text;
  project_key text;
  binding_source text;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  state := value->>'state';
  project_key := value->>'project_key';
  binding_source := value->>'binding_source';
  RETURN key_count = 3
    AND value ?& ARRAY['state', 'project_key', 'binding_source']
    AND state IN ('not_applicable', 'resolved', 'unresolved')
    AND binding_source IN (
      'not_applicable', 'explicit_source_text',
      'trusted_thread_binding', 'unresolved'
    )
    AND (
      (state = 'not_applicable'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'not_applicable')
      OR
      (state = 'unresolved'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'unresolved')
      OR
      (state = 'resolved'
       AND project_key IS NOT NULL
       AND btrim(project_key) <> ''
       AND length(project_key) <= 500
       AND binding_source IN ('explicit_source_text', 'trusted_thread_binding'))
    );
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_literal_object_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  RETURN key_count = 5
    AND value ?& ARRAY['kind', 'datatype', 'value', 'unit', 'approximate']
    AND value->>'kind' = 'literal'
    AND value->>'datatype' IN (
      'text', 'number', 'boolean', 'date', 'duration', 'location', 'enum', 'json'
    )
    AND (
      value->'unit' = 'null'::jsonb
      OR (jsonb_typeof(value->'unit') = 'string'
          AND btrim(value->>'unit') <> ''
          AND length(value->>'unit') <= 500)
    )
    AND jsonb_typeof(value->'approximate') = 'boolean';
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_proposed_entity_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
  identity_state text;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  identity_state := value->>'identity_state';
  RETURN key_count = 5
    AND value ?& ARRAY[
      'entity_type', 'identity_state', 'canonical_name',
      'display_label', 'creation_reason'
    ]
    AND value->>'entity_type' IN (
      'person', 'animal', 'organization', 'place', 'object', 'concept'
    )
    AND identity_state IN ('named', 'role_only', 'anonymous')
    AND jsonb_typeof(value->'display_label') = 'string'
    AND btrim(value->>'display_label') <> ''
    AND length(value->>'display_label') <= 300
    AND (value->>'creation_reason') ~ '^[a-z][a-z0-9_]{1,99}$'
    AND (
      (identity_state = 'named'
       AND jsonb_typeof(value->'canonical_name') = 'string'
       AND btrim(value->>'canonical_name') <> ''
       AND length(value->>'canonical_name') <= 500)
      OR
      (identity_state IN ('role_only', 'anonymous')
       AND value->'canonical_name' = 'null'::jsonb)
    );
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_candidate_features_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
  key text;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  IF key_count <> 9 OR NOT (value ?& ARRAY[
    'active_status', 'entity_type_match', 'exact_canonical_name',
    'exact_alias', 'relationship_role_supported', 'source_local_coreference',
    'graph_neighbor_supported', 'conflicting_attribute_count',
    'same_name_candidate_count'
  ]) THEN
    RETURN false;
  END IF;
  FOREACH key IN ARRAY ARRAY[
    'active_status', 'entity_type_match', 'exact_canonical_name',
    'exact_alias', 'relationship_role_supported', 'source_local_coreference',
    'graph_neighbor_supported'
  ]
  LOOP
    IF jsonb_typeof(value->key) <> 'boolean' THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN jsonb_typeof(value->'conflicting_attribute_count') = 'number'
    AND jsonb_typeof(value->'same_name_candidate_count') = 'number'
    AND (value->>'conflicting_attribute_count')::numeric
        = trunc((value->>'conflicting_attribute_count')::numeric)
    AND (value->>'same_name_candidate_count')::numeric
        = trunc((value->>'same_name_candidate_count')::numeric)
    AND (value->>'conflicting_attribute_count')::integer >= 0
    AND (value->>'same_name_candidate_count')::integer >= 0;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_relative_offset_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  RETURN key_count = 5
    AND value ?& ARRAY[
      'direction', 'magnitude', 'unit', 'approximate', 'anchor_source'
    ]
    AND value->>'direction' IN ('past', 'future')
    AND jsonb_typeof(value->'magnitude') = 'number'
    AND (value->>'magnitude')::numeric > 0
    AND value->>'unit' IN ('minute', 'hour', 'day', 'week', 'month', 'year')
    AND jsonb_typeof(value->'approximate') = 'boolean'
    AND value->>'anchor_source' = 'evidence_observed_at';
EXCEPTION WHEN invalid_text_representation THEN
  RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_recurrence_valid(value jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
  SELECT value = '{"kind":"unspecified_repeated"}'::jsonb
$function$;

CREATE TEMP TABLE v5_predicate_seed_expected(
  predicate text PRIMARY KEY,
  object_kind text NOT NULL,
  cardinality text NOT NULL,
  description text NOT NULL,
  lifecycle text NOT NULL,
  extraction_allowed boolean NOT NULL,
  successor_predicates text[] NOT NULL
) ON COMMIT DROP;

INSERT INTO v5_predicate_seed_expected VALUES
  ('age.reported', 'literal', 'many', 'Age as reported by the user at an observation time, with an explicit unit.', 'active', true, '{}'),
  ('credential.reported', 'literal', 'many', 'A credential explicitly reported by the user; not independently verified.', 'active', true, '{}'),
  ('health.user_reported_observation', 'literal', 'many', 'A health-related observation attributed to the user report, not medical truth.', 'active', true, '{}'),
  ('health.user_reported_uncertain_label', 'literal', 'many', 'An explicitly uncertain health or diagnostic label reported by the user.', 'active', true, '{}'),
  ('identity.name', 'literal', 'many', 'A name explicitly stated for an entity.', 'active', true, '{}'),
  ('identity.name_canonical', 'literal', 'one', 'The canonical name asserted through an explicit correction or normalization.', 'active', true, '{}'),
  ('life_event.died', 'literal', 'one', 'A death event reported for a person or animal, with occurrence time when supported.', 'active', true, '{}'),
  ('occupation.works_as', 'entity', 'many', 'An occupation role explicitly stated for a person.', 'active', true, '{}'),
  ('pet.breed', 'literal', 'one', 'A breed or mix explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.coat_color', 'literal', 'many', 'A coat color explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.eye_color', 'literal', 'many', 'An eye color explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.hearing_status', 'literal', 'one', 'A hearing status explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.sex', 'literal', 'one', 'Sex explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.species', 'literal', 'one', 'Species explicitly reported for an animal.', 'active', true, '{}'),
  ('pet.weight_reported', 'literal', 'many', 'A weight reported for an animal with kg or lb as the explicit unit.', 'active', true, '{}'),
  ('preference.life', 'literal', 'many', 'A stable non-response preference explicitly expressed by the user.', 'active', true, '{}'),
  ('preference.response', 'literal', 'many', 'A stable direct instruction controlling response behavior; never prompt content.', 'active', true, '{}'),
  ('project.constraint', 'literal', 'many', 'A constraint within an owner-registered project.', 'active', true, '{}'),
  ('project.current_state', 'literal', 'many', 'A current state assertion within an owner-registered project.', 'active', true, '{}'),
  ('project.proposed_feature', 'literal', 'many', 'A proposed or planned feature within an owner-registered project.', 'active', true, '{}'),
  ('project.requirement', 'literal', 'many', 'A requirement within an owner-registered project.', 'active', true, '{}'),
  ('relationship.has_pet', 'entity', 'many', 'A person-to-animal pet relationship.', 'active', true, '{}'),
  ('relationship.parent_of', 'entity', 'many', 'A directed parent-to-child relationship.', 'active', true, '{}'),
  ('relationship.sibling_of', 'entity', 'many', 'A symmetric sibling relationship between two people.', 'active', true, '{}'),
  ('residence.lives_at', 'entity', 'many', 'A residence relationship to a place, with a validity interval when supported.', 'active', true, '{}'),
  ('did_activity_in_past', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('does_activity', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('financial_status', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('goal', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('has_children_names', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, ARRAY['identity.name', 'relationship.parent_of']),
  ('has_name', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, ARRAY['identity.name']),
  ('identity.preferred_name', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('life_context.active', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('name.canonical', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, ARRAY['identity.name_canonical']),
  ('often_takes_walks', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('personal_event.occurred', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('project.current', 'entity', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('raises', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('relationship.kind', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('spends_time_at', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('spends_time_on', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('spent_time_in', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('spouse_name', 'literal', 'one', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}'),
  ('stopped_alcohol_use', 'literal', 'many', 'Legacy compatibility predicate retained during cutover.', 'legacy_read_only', false, '{}');

CREATE TEMP TABLE v5_predicate_preexisting ON COMMIT DROP AS
SELECT predicate FROM memory.predicate
WHERE predicate IN (SELECT predicate FROM v5_predicate_seed_expected);

INSERT INTO memory.predicate(predicate, object_kind, cardinality, description, active)
SELECT predicate, object_kind, cardinality, description, true
FROM v5_predicate_seed_expected
ON CONFLICT (predicate) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory.predicate_registry_version (
  registry_version text PRIMARY KEY,
  contract_version text NOT NULL,
  status text NOT NULL,
  runtime_active boolean NOT NULL DEFAULT false,
  unknown_predicate_action text NOT NULL,
  registry_sha256 text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (status IN ('proposed', 'active', 'retired')),
  CHECK (unknown_predicate_action = 'defer_unregistered_predicate'),
  CHECK (memory.v5_sha256_valid(registry_sha256))
);

CREATE TABLE IF NOT EXISTS memory.predicate_registry_seed (
  registry_version text NOT NULL
    REFERENCES memory.predicate_registry_version(registry_version) ON DELETE CASCADE,
  predicate text NOT NULL REFERENCES memory.predicate(predicate) ON DELETE RESTRICT,
  base_predicate_created boolean NOT NULL,
  PRIMARY KEY (registry_version, predicate)
);

CREATE TABLE IF NOT EXISTS memory.predicate_contract (
  predicate text NOT NULL REFERENCES memory.predicate(predicate) ON DELETE RESTRICT,
  registry_version text NOT NULL
    REFERENCES memory.predicate_registry_version(registry_version) ON DELETE RESTRICT,
  lifecycle text NOT NULL,
  extraction_allowed boolean NOT NULL,
  object_kind text NOT NULL,
  cardinality text NOT NULL,
  successor_predicates text[] NOT NULL DEFAULT '{}',
  contract jsonb NOT NULL,
  contract_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (predicate, registry_version),
  CHECK (lifecycle IN ('active', 'legacy_read_only')),
  CHECK (object_kind IN ('entity', 'literal')),
  CHECK (cardinality IN ('one', 'many')),
  CHECK ((lifecycle = 'active') = extraction_allowed),
  CHECK (array_position(successor_predicates, NULL) IS NULL),
  CHECK (jsonb_typeof(contract) = 'object'),
  CHECK (memory.v5_sha256_valid(contract_sha256))
);

INSERT INTO memory.predicate_registry_version(
  registry_version, contract_version, status, runtime_active,
  unknown_predicate_action, registry_sha256
) VALUES (
  'memory_predicate_registry_v5',
  'memory_v1_relational_extraction_v5',
  'proposed',
  false,
  'defer_unregistered_predicate',
  '4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4'
)
ON CONFLICT (registry_version) DO NOTHING;

INSERT INTO memory.predicate_registry_seed(
  registry_version, predicate, base_predicate_created
)
SELECT
  'memory_predicate_registry_v5',
  expected.predicate,
  preexisting.predicate IS NULL
FROM v5_predicate_seed_expected AS expected
LEFT JOIN v5_predicate_preexisting AS preexisting USING (predicate)
ON CONFLICT (registry_version, predicate) DO NOTHING;

INSERT INTO memory.predicate_contract(
  predicate, registry_version, lifecycle, extraction_allowed,
  object_kind, cardinality, successor_predicates, contract, contract_sha256
)
SELECT
  expected.predicate,
  'memory_predicate_registry_v5',
  expected.lifecycle,
  expected.extraction_allowed,
  expected.object_kind,
  expected.cardinality,
  expected.successor_predicates,
  jsonb_build_object(
    'predicate', expected.predicate,
    'lifecycle', expected.lifecycle,
    'extraction_allowed', expected.extraction_allowed,
    'object_kind', expected.object_kind,
    'cardinality', expected.cardinality,
    'successor_predicates', to_jsonb(expected.successor_predicates)
  ),
  encode(digest(convert_to(
    concat_ws('|', expected.predicate, expected.lifecycle,
      expected.extraction_allowed::text, expected.object_kind,
      expected.cardinality, array_to_string(expected.successor_predicates, ',')),
    'UTF8'), 'sha256'), 'hex')
FROM v5_predicate_seed_expected AS expected
ON CONFLICT (predicate, registry_version) DO NOTHING;

DO $block$
DECLARE
  mismatch_count integer;
BEGIN
  SELECT count(*) INTO mismatch_count
  FROM v5_predicate_seed_expected AS expected
  JOIN memory.predicate AS base USING (predicate)
  JOIN memory.predicate_contract AS contract
    ON contract.predicate = expected.predicate
   AND contract.registry_version = 'memory_predicate_registry_v5'
  WHERE base.object_kind <> expected.object_kind
     OR base.cardinality <> expected.cardinality
     OR NOT base.active
     OR contract.lifecycle <> expected.lifecycle
     OR contract.extraction_allowed <> expected.extraction_allowed
     OR contract.object_kind <> expected.object_kind
     OR contract.cardinality <> expected.cardinality
     OR contract.successor_predicates <> expected.successor_predicates;
  IF mismatch_count <> 0
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5') <> 44
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5'
           AND lifecycle = 'active' AND extraction_allowed) <> 25
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = 'memory_predicate_registry_v5'
           AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed) <> 19 THEN
    RAISE EXCEPTION 'memory V5 predicate registry conflict'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = 'memory_predicate_registry_v5'
      AND (
        contract_version <> 'memory_v1_relational_extraction_v5'
        OR status <> 'proposed'
        OR runtime_active
        OR unknown_predicate_action <> 'defer_unregistered_predicate'
        OR registry_sha256 <> '4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4'
      )
  ) THEN
    RAISE EXCEPTION 'memory V5 registry version metadata conflict'
      USING ERRCODE = '23514';
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS memory.entity_mention (
  mention_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  packet_sha256 text NOT NULL,
  entity_ref text NOT NULL,
  entity_type text NOT NULL,
  mention_kind memory.entity_mention_kind NOT NULL,
  name_text text,
  relationship_role text,
  source_spans jsonb NOT NULL,
  extraction_confidence numeric(4,3) NOT NULL,
  reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  mention_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, mention_id),
  UNIQUE (owner_user_id, evidence_id, mention_id),
  UNIQUE (owner_user_id, evidence_id, packet_sha256, entity_ref),
  UNIQUE (owner_user_id, evidence_id, mention_sha256),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id) ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(packet_sha256)),
  CHECK (entity_ref ~ '^e[0-9]{2}$'),
  CHECK (entity_type IN (
    'self', 'person', 'animal', 'organization',
    'place', 'project', 'object', 'concept'
  )),
  CHECK (extraction_confidence BETWEEN 0 AND 1),
  CHECK (btrim(extractor) <> '' AND length(extractor) <= 120),
  CHECK (btrim(extractor_version) <> '' AND length(extractor_version) <= 120),
  CHECK (memory.v5_sha256_valid(mention_sha256)),
  CHECK (memory.v5_source_spans_valid(source_spans)),
  CHECK (memory.v5_reason_codes_valid(reason_codes, 20)),
  CHECK (
    (mention_kind = 'self_reference'
     AND entity_type = 'self' AND name_text IS NULL AND relationship_role IS NULL)
    OR
    (mention_kind = 'named'
     AND name_text IS NOT NULL AND btrim(name_text) <> ''
     AND length(name_text) <= 500)
    OR
    (mention_kind = 'role_only'
     AND name_text IS NULL AND relationship_role IS NOT NULL
     AND btrim(relationship_role) <> '' AND length(relationship_role) <= 500)
    OR
    (mention_kind = 'anonymous' AND name_text IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS memory.entity_resolution_plan (
  resolution_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  mention_id uuid NOT NULL,
  predicate_registry_version text NOT NULL,
  entity_normalization_version text NOT NULL,
  resolver text NOT NULL,
  resolver_version text NOT NULL,
  action memory.entity_resolution_action NOT NULL,
  decision_state memory.entity_resolution_state NOT NULL,
  selected_entity_id uuid,
  proposed_entity jsonb,
  candidate_set_sha256 text NOT NULL,
  decision_sha256 text NOT NULL,
  review_reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, resolution_id),
  UNIQUE (owner_user_id, evidence_id, mention_id, decision_sha256),
  FOREIGN KEY (owner_user_id, evidence_id, mention_id)
    REFERENCES memory.entity_mention(owner_user_id, evidence_id, mention_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, selected_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  FOREIGN KEY (predicate_registry_version)
    REFERENCES memory.predicate_registry_version(registry_version) ON DELETE RESTRICT,
  CHECK (predicate_registry_version = 'memory_predicate_registry_v5'),
  CHECK (entity_normalization_version = 'memory_entity_normalization_v5'),
  CHECK (btrim(resolver) <> '' AND length(resolver) <= 120),
  CHECK (btrim(resolver_version) <> '' AND length(resolver_version) <= 120),
  CHECK (memory.v5_sha256_valid(candidate_set_sha256)),
  CHECK (memory.v5_sha256_valid(decision_sha256)),
  CHECK (memory.v5_reason_codes_valid(review_reason_codes, 20)),
  CHECK (proposed_entity IS NULL OR memory.v5_proposed_entity_valid(proposed_entity)),
  CHECK (
    (action = 'link_existing'
     AND selected_entity_id IS NOT NULL AND proposed_entity IS NULL
     AND decision_state IN ('auto_link_eligible', 'manual_review_required'))
    OR
    (action = 'create_new'
     AND selected_entity_id IS NULL AND proposed_entity IS NOT NULL
     AND decision_state = 'manual_review_required')
    OR
    (action = 'defer'
     AND selected_entity_id IS NULL AND proposed_entity IS NULL
     AND decision_state = 'deferred')
    OR
    (action = 'reject'
     AND selected_entity_id IS NULL AND proposed_entity IS NULL
     AND decision_state = 'rejected')
  )
);

CREATE TABLE IF NOT EXISTS memory.entity_resolution_candidate (
  owner_user_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  candidate_entity_id uuid NOT NULL,
  ordinal smallint NOT NULL,
  entity_type text NOT NULL,
  features jsonb NOT NULL,
  exclusion_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, resolution_id, candidate_entity_id),
  UNIQUE (owner_user_id, resolution_id, ordinal),
  FOREIGN KEY (owner_user_id, resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id, resolution_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, candidate_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CHECK (ordinal BETWEEN 1 AND 20),
  CHECK (entity_type IN (
    'self', 'person', 'animal', 'organization',
    'place', 'project', 'object', 'concept'
  )),
  CHECK (memory.v5_candidate_features_valid(features)),
  CHECK (memory.v5_reason_codes_valid(exclusion_reasons, 20))
);

CREATE TABLE IF NOT EXISTS memory.entity_resolution_review (
  review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  decision memory.entity_review_decision NOT NULL,
  expected_mention_sha256 text NOT NULL,
  expected_candidate_set_sha256 text NOT NULL,
  expected_decision_sha256 text NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text,
  reason text NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, review_id),
  UNIQUE (owner_user_id, resolution_id, review_id),
  UNIQUE (owner_user_id, resolution_id, authorization_manifest_sha256),
  FOREIGN KEY (owner_user_id, resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id, resolution_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(expected_mention_sha256)),
  CHECK (memory.v5_sha256_valid(expected_candidate_set_sha256)),
  CHECK (memory.v5_sha256_valid(expected_decision_sha256)),
  CHECK (reviewer_type IN ('user', 'system', 'job', 'admin')),
  CHECK (reviewer_ref IS NULL OR length(reviewer_ref) <= 500),
  CHECK (btrim(reason) <> '' AND length(reason) <= 2000),
  CHECK (memory.v5_sha256_valid(authorization_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.entity_resolution_apply (
  owner_user_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  review_id uuid,
  applied_entity_id uuid NOT NULL,
  apply_manifest_sha256 text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, resolution_id),
  UNIQUE (owner_user_id, resolution_id, applied_entity_id),
  UNIQUE (owner_user_id, apply_manifest_sha256),
  FOREIGN KEY (owner_user_id, resolution_id)
    REFERENCES memory.entity_resolution_plan(owner_user_id, resolution_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resolution_id, review_id)
    REFERENCES memory.entity_resolution_review(owner_user_id, resolution_id, review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, applied_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(apply_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.entity_alias_observation (
  alias_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  mention_id uuid NOT NULL,
  resolution_id uuid NOT NULL,
  entity_id uuid NOT NULL,
  alias_text text NOT NULL,
  normalized_alias text NOT NULL,
  alias_type text NOT NULL,
  source_spans jsonb NOT NULL,
  normalization_version text NOT NULL,
  alias_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, alias_observation_id),
  UNIQUE (owner_user_id, evidence_id, alias_sha256),
  FOREIGN KEY (owner_user_id, evidence_id, mention_id)
    REFERENCES memory.entity_mention(owner_user_id, evidence_id, mention_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, resolution_id, entity_id)
    REFERENCES memory.entity_resolution_apply(
      owner_user_id, resolution_id, applied_entity_id
    ) ON DELETE RESTRICT,
  CHECK (btrim(alias_text) <> '' AND length(alias_text) <= 500),
  CHECK (btrim(normalized_alias) <> '' AND length(normalized_alias) <= 500),
  CHECK (alias_type IN ('observed_name', 'explicit_correction', 'transcription_variant')),
  CHECK (normalization_version = 'memory_entity_normalization_v5'),
  CHECK (memory.v5_source_spans_valid(source_spans)),
  CHECK (memory.v5_sha256_valid(alias_sha256))
);

CREATE TABLE IF NOT EXISTS memory.observation (
  observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  observation_ref text NOT NULL,
  subject_mention_id uuid NOT NULL,
  predicate text NOT NULL,
  predicate_registry_version text NOT NULL,
  object_mention_id uuid,
  object_literal jsonb,
  polarity memory.observation_polarity NOT NULL,
  modality memory.observation_modality NOT NULL,
  projection_class memory.observation_projection_class NOT NULL,
  surface_policy memory.observation_surface_policy NOT NULL,
  project_scope jsonb NOT NULL,
  sensitivity memory.sensitivity_level NOT NULL,
  extraction_confidence numeric(4,3) NOT NULL,
  source_spans jsonb NOT NULL,
  reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  packet_sha256 text NOT NULL,
  observation_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, observation_id),
  UNIQUE (owner_user_id, evidence_id, observation_id),
  UNIQUE (owner_user_id, evidence_id, packet_sha256, observation_ref),
  UNIQUE (owner_user_id, evidence_id, observation_sha256),
  FOREIGN KEY (owner_user_id, evidence_id, subject_mention_id)
    REFERENCES memory.entity_mention(owner_user_id, evidence_id, mention_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id, object_mention_id)
    REFERENCES memory.entity_mention(owner_user_id, evidence_id, mention_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (predicate, predicate_registry_version)
    REFERENCES memory.predicate_contract(predicate, registry_version)
    ON DELETE RESTRICT,
  CHECK (observation_ref ~ '^o[0-9]{2}$'),
  CHECK ((object_mention_id IS NULL) <> (object_literal IS NULL)),
  CHECK (object_literal IS NULL OR memory.v5_literal_object_valid(object_literal)),
  CHECK (object_mention_id IS NULL OR object_mention_id <> subject_mention_id),
  CHECK (memory.v5_project_scope_valid(project_scope)),
  CHECK (extraction_confidence BETWEEN 0 AND 1),
  CHECK (memory.v5_source_spans_valid(source_spans)),
  CHECK (memory.v5_reason_codes_valid(reason_codes, 20)),
  CHECK (btrim(extractor) <> '' AND length(extractor) <= 120),
  CHECK (btrim(extractor_version) <> '' AND length(extractor_version) <= 120),
  CHECK (memory.v5_sha256_valid(packet_sha256)),
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (
    (projection_class = 'never_surface' AND surface_policy = 'never')
    OR projection_class <> 'never_surface'
  ),
  CHECK (
    (projection_class = 'response_preference'
     AND surface_policy = 'zero_token_control_only')
    OR projection_class <> 'response_preference'
  )
);

CREATE TABLE IF NOT EXISTS memory.observation_temporal (
  owner_user_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  semantic memory.temporal_semantic NOT NULL,
  shape memory.temporal_shape NOT NULL,
  basis memory.temporal_basis NOT NULL,
  source_form memory.temporal_source_form NOT NULL,
  certainty memory.temporal_certainty NOT NULL,
  precision memory.temporal_precision NOT NULL,
  instant_at timestamptz,
  calendar_range daterange,
  instant_range tstzrange,
  relative_offset jsonb,
  recurrence jsonb,
  anchored_to_source_time boolean NOT NULL,
  normalization_policy_version text NOT NULL,
  reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  normalized_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, observation_id),
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  CHECK (normalization_policy_version = 'memory_temporal_normalization_v5'),
  CHECK (memory.v5_reason_codes_valid(reason_codes, 10)),
  CHECK (memory.v5_sha256_valid(normalized_sha256)),
  CHECK (
    calendar_range IS NULL OR (
      NOT isempty(calendar_range)
      AND (lower_inf(calendar_range) OR lower_inc(calendar_range))
      AND (upper_inf(calendar_range) OR NOT upper_inc(calendar_range))
    )
  ),
  CHECK (
    instant_range IS NULL OR (
      NOT isempty(instant_range)
      AND (lower_inf(instant_range) OR lower_inc(instant_range))
      AND (upper_inf(instant_range) OR NOT upper_inc(instant_range))
    )
  ),
  CHECK (
    (basis = 'none'
     AND semantic = 'none' AND shape = 'none' AND source_form = 'none'
     AND certainty = 'unknown' AND precision = 'unknown'
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NULL AND recurrence IS NULL
     AND NOT anchored_to_source_time)
    OR
    (basis = 'instant'
     AND semantic <> 'none'
     AND source_form IN ('absolute', 'implicit_source_time')
     AND precision IN ('exact', 'minute')
     AND calendar_range IS NULL AND relative_offset IS NULL AND recurrence IS NULL
     AND (
       (shape = 'instant' AND instant_at IS NOT NULL AND instant_range IS NULL)
       OR
       (shape = 'bounded_interval' AND instant_at IS NULL
        AND instant_range IS NOT NULL
        AND NOT lower_inf(instant_range) AND NOT upper_inf(instant_range))
       OR
       (shape = 'open_interval' AND instant_at IS NULL
        AND instant_range IS NOT NULL
        AND (lower_inf(instant_range) <> upper_inf(instant_range)))
     )
     AND (anchored_to_source_time = (source_form = 'implicit_source_time')))
    OR
    (basis = 'calendar'
     AND semantic <> 'none'
     AND shape IN ('instant', 'bounded_interval', 'open_interval')
     AND source_form IN ('absolute', 'partial_absolute')
     AND precision IN ('day', 'month', 'year')
     AND instant_at IS NULL AND calendar_range IS NOT NULL
     AND instant_range IS NULL AND relative_offset IS NULL AND recurrence IS NULL
     AND NOT anchored_to_source_time
     AND (
       (shape IN ('instant', 'bounded_interval')
        AND NOT lower_inf(calendar_range) AND NOT upper_inf(calendar_range))
       OR
       (shape = 'open_interval'
        AND (lower_inf(calendar_range) <> upper_inf(calendar_range)))
     ))
    OR
    (basis = 'relative'
     AND semantic <> 'none' AND shape = 'instant' AND source_form = 'relative'
     AND precision IN ('minute', 'day', 'month', 'year', 'unknown')
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NOT NULL
     AND memory.v5_relative_offset_valid(relative_offset)
     AND recurrence IS NULL AND anchored_to_source_time)
    OR
    (basis = 'recurring'
     AND semantic <> 'none' AND shape = 'recurring' AND source_form = 'none'
     AND precision = 'unknown'
     AND instant_at IS NULL AND calendar_range IS NULL AND instant_range IS NULL
     AND relative_offset IS NULL AND recurrence IS NOT NULL
     AND memory.v5_recurrence_valid(recurrence)
     AND NOT anchored_to_source_time)
  )
);

CREATE TABLE IF NOT EXISTS memory.observation_entity_binding (
  owner_user_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  subject_resolution_id uuid NOT NULL,
  subject_entity_id uuid NOT NULL,
  object_resolution_id uuid,
  object_entity_id uuid,
  binding_manifest_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, observation_id),
  UNIQUE (owner_user_id, binding_manifest_sha256),
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, subject_resolution_id, subject_entity_id)
    REFERENCES memory.entity_resolution_apply(
      owner_user_id, resolution_id, applied_entity_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, object_resolution_id, object_entity_id)
    REFERENCES memory.entity_resolution_apply(
      owner_user_id, resolution_id, applied_entity_id
    ) ON DELETE RESTRICT,
  CHECK ((object_resolution_id IS NULL) = (object_entity_id IS NULL)),
  CHECK (memory.v5_sha256_valid(binding_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.claim_observation (
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL,
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, claim_id, observation_id, stance),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id) ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS memory.candidate_observation (
  owner_user_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, candidate_id, observation_id),
  FOREIGN KEY (owner_user_id, candidate_id)
    REFERENCES memory.candidate(owner_user_id, candidate_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, observation_id)
    REFERENCES memory.observation(owner_user_id, observation_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS entity_mention_owner_evidence_idx
  ON memory.entity_mention(owner_user_id, evidence_id, created_at);
CREATE INDEX IF NOT EXISTS entity_resolution_plan_owner_state_idx
  ON memory.entity_resolution_plan(owner_user_id, decision_state, created_at);
CREATE INDEX IF NOT EXISTS entity_alias_observation_owner_alias_idx
  ON memory.entity_alias_observation(owner_user_id, normalized_alias);
CREATE INDEX IF NOT EXISTS observation_owner_predicate_idx
  ON memory.observation(owner_user_id, predicate, created_at);
CREATE INDEX IF NOT EXISTS observation_owner_evidence_idx
  ON memory.observation(owner_user_id, evidence_id, created_at);

CREATE OR REPLACE FUNCTION memory.guard_v5_actor_active_evidence()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  evidence_status memory.record_status;
BEGIN
  IF memory.current_actor_user_id() IS NULL
     OR NEW.owner_user_id <> memory.current_actor_user_id() THEN
    RAISE EXCEPTION 'V5 staging owner does not match current actor'
      USING ERRCODE = '42501';
  END IF;
  SELECT evidence.status INTO evidence_status
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = NEW.owner_user_id
    AND evidence.evidence_id = NEW.evidence_id;
  IF NOT FOUND OR evidence_status <> 'active' THEN
    RAISE EXCEPTION 'V5 staging requires active owner-scoped evidence'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_resolution_plan()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  mention_type text;
BEGIN
  SELECT mention.entity_type INTO mention_type
  FROM memory.entity_mention AS mention
  WHERE mention.owner_user_id = NEW.owner_user_id
    AND mention.evidence_id = NEW.evidence_id
    AND mention.mention_id = NEW.mention_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped mention not found'
      USING ERRCODE = '23503';
  END IF;
  IF NEW.action = 'create_new' AND mention_type IN ('self', 'project') THEN
    RAISE EXCEPTION 'self and project entities cannot be model-created'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_observation_contract()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  contract_kind text;
  can_extract boolean;
BEGIN
  SELECT contract.object_kind, contract.extraction_allowed
  INTO contract_kind, can_extract
  FROM memory.predicate_contract AS contract
  WHERE contract.predicate = NEW.predicate
    AND contract.registry_version = NEW.predicate_registry_version;
  IF NOT FOUND OR NOT can_extract THEN
    RAISE EXCEPTION 'predicate is not extraction-enabled in V5: %', NEW.predicate
      USING ERRCODE = '23514';
  END IF;
  IF (contract_kind = 'entity') <> (NEW.object_mention_id IS NOT NULL) THEN
    RAISE EXCEPTION 'predicate object kind mismatch for %', NEW.predicate
      USING ERRCODE = '23514';
  END IF;
  IF NEW.predicate LIKE 'project.%' THEN
    IF NEW.project_scope->>'state' <> 'resolved' THEN
      RAISE EXCEPTION 'project predicate requires trusted resolved project scope'
        USING ERRCODE = '23514';
    END IF;
    IF NEW.projection_class <> 'project_knowledge'
       OR NEW.surface_policy <> 'exact_project_scope_only' THEN
      RAISE EXCEPTION 'project predicate requires project-only projection policy'
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.project_scope->>'state' <> 'not_applicable' THEN
    RAISE EXCEPTION 'non-project predicate cannot carry project scope'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_resolution_review()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  plan memory.entity_resolution_plan%ROWTYPE;
  mention_sha text;
BEGIN
  SELECT * INTO plan FROM memory.entity_resolution_plan
  WHERE owner_user_id = NEW.owner_user_id
    AND resolution_id = NEW.resolution_id;
  SELECT mention.mention_sha256 INTO mention_sha
  FROM memory.entity_mention AS mention
  WHERE mention.owner_user_id = plan.owner_user_id
    AND mention.mention_id = plan.mention_id;
  IF NEW.expected_mention_sha256 <> mention_sha
     OR NEW.expected_candidate_set_sha256 <> plan.candidate_set_sha256
     OR NEW.expected_decision_sha256 <> plan.decision_sha256 THEN
    RAISE EXCEPTION 'resolution review hash lock mismatch'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.decision = 'approved'
     AND plan.decision_state <> 'manual_review_required' THEN
    RAISE EXCEPTION 'only manual-review plans may be explicitly approved'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_resolution_apply()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  plan memory.entity_resolution_plan%ROWTYPE;
  mention_type text;
  review_decision memory.entity_review_decision;
  candidate_features jsonb;
BEGIN
  SELECT * INTO plan FROM memory.entity_resolution_plan
  WHERE owner_user_id = NEW.owner_user_id
    AND resolution_id = NEW.resolution_id;
  SELECT mention.entity_type INTO mention_type
  FROM memory.entity_mention AS mention
  WHERE mention.owner_user_id = plan.owner_user_id
    AND mention.mention_id = plan.mention_id;

  IF plan.decision_state = 'auto_link_eligible' THEN
    IF NEW.review_id IS NOT NULL
       OR plan.action <> 'link_existing'
       OR plan.selected_entity_id <> NEW.applied_entity_id THEN
      RAISE EXCEPTION 'invalid auto-link application'
        USING ERRCODE = '23514';
    END IF;
    SELECT candidate.features INTO candidate_features
    FROM memory.entity_resolution_candidate AS candidate
    WHERE candidate.owner_user_id = NEW.owner_user_id
      AND candidate.resolution_id = NEW.resolution_id
      AND candidate.candidate_entity_id = NEW.applied_entity_id;
    IF NOT FOUND
       OR NOT (candidate_features->>'active_status')::boolean
       OR NOT (candidate_features->>'entity_type_match')::boolean
       OR (candidate_features->>'conflicting_attribute_count')::integer <> 0
       OR NOT (
         mention_type = 'self'
         OR (
           (candidate_features->>'same_name_candidate_count')::integer = 1
           AND (
             (candidate_features->>'exact_canonical_name')::boolean
             OR (candidate_features->>'exact_alias')::boolean
           )
         )
       ) THEN
      RAISE EXCEPTION 'auto-link candidate is not uniquely trusted'
        USING ERRCODE = '23514';
    END IF;
  ELSIF plan.decision_state = 'manual_review_required' THEN
    IF NEW.review_id IS NULL THEN
      RAISE EXCEPTION 'manual resolution requires an approved review'
        USING ERRCODE = '23514';
    END IF;
    SELECT review.decision INTO review_decision
    FROM memory.entity_resolution_review AS review
    WHERE review.owner_user_id = NEW.owner_user_id
      AND review.resolution_id = NEW.resolution_id
      AND review.review_id = NEW.review_id;
    IF review_decision <> 'approved' THEN
      RAISE EXCEPTION 'resolution review is not approved'
        USING ERRCODE = '23514';
    END IF;
    IF plan.action = 'link_existing'
       AND plan.selected_entity_id <> NEW.applied_entity_id THEN
      RAISE EXCEPTION 'reviewed link target changed'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    RAISE EXCEPTION 'deferred or rejected resolution cannot be applied'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_observation_binding()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  staged memory.observation%ROWTYPE;
  subject_mention uuid;
  object_mention uuid;
BEGIN
  SELECT * INTO staged FROM memory.observation
  WHERE owner_user_id = NEW.owner_user_id
    AND observation_id = NEW.observation_id;
  SELECT plan.mention_id INTO subject_mention
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_resolution_apply AS applied
    ON applied.owner_user_id = plan.owner_user_id
   AND applied.resolution_id = plan.resolution_id
  WHERE plan.owner_user_id = NEW.owner_user_id
    AND plan.resolution_id = NEW.subject_resolution_id
    AND applied.applied_entity_id = NEW.subject_entity_id;
  IF subject_mention IS DISTINCT FROM staged.subject_mention_id THEN
    RAISE EXCEPTION 'subject binding does not resolve the staged mention'
      USING ERRCODE = '23514';
  END IF;
  IF staged.object_mention_id IS NULL THEN
    IF NEW.object_resolution_id IS NOT NULL OR NEW.object_entity_id IS NOT NULL THEN
      RAISE EXCEPTION 'literal observation cannot have object entity binding'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    SELECT plan.mention_id INTO object_mention
    FROM memory.entity_resolution_plan AS plan
    JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id = plan.owner_user_id
     AND applied.resolution_id = plan.resolution_id
    WHERE plan.owner_user_id = NEW.owner_user_id
      AND plan.resolution_id = NEW.object_resolution_id
      AND applied.applied_entity_id = NEW.object_entity_id;
    IF object_mention IS DISTINCT FROM staged.object_mention_id THEN
      RAISE EXCEPTION 'object binding does not resolve the staged mention'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'memory V5 staging rows are append-only'
    USING ERRCODE = '42501';
END
$function$;

DROP TRIGGER IF EXISTS entity_mention_active_evidence_guard ON memory.entity_mention;
CREATE TRIGGER entity_mention_active_evidence_guard
BEFORE INSERT ON memory.entity_mention
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_actor_active_evidence();

DROP TRIGGER IF EXISTS entity_resolution_plan_active_evidence_guard
  ON memory.entity_resolution_plan;
CREATE TRIGGER entity_resolution_plan_active_evidence_guard
BEFORE INSERT ON memory.entity_resolution_plan
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_actor_active_evidence();
DROP TRIGGER IF EXISTS entity_resolution_plan_contract_guard
  ON memory.entity_resolution_plan;
CREATE TRIGGER entity_resolution_plan_contract_guard
BEFORE INSERT ON memory.entity_resolution_plan
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_resolution_plan();

DROP TRIGGER IF EXISTS entity_alias_observation_active_evidence_guard
  ON memory.entity_alias_observation;
CREATE TRIGGER entity_alias_observation_active_evidence_guard
BEFORE INSERT ON memory.entity_alias_observation
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_actor_active_evidence();

DROP TRIGGER IF EXISTS observation_active_evidence_guard ON memory.observation;
CREATE TRIGGER observation_active_evidence_guard
BEFORE INSERT ON memory.observation
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_actor_active_evidence();
DROP TRIGGER IF EXISTS observation_contract_guard ON memory.observation;
CREATE TRIGGER observation_contract_guard
BEFORE INSERT ON memory.observation
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_observation_contract();

DROP TRIGGER IF EXISTS entity_resolution_review_hash_guard
  ON memory.entity_resolution_review;
CREATE TRIGGER entity_resolution_review_hash_guard
BEFORE INSERT ON memory.entity_resolution_review
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_resolution_review();

DROP TRIGGER IF EXISTS entity_resolution_apply_guard
  ON memory.entity_resolution_apply;
CREATE TRIGGER entity_resolution_apply_guard
BEFORE INSERT ON memory.entity_resolution_apply
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_resolution_apply();

DROP TRIGGER IF EXISTS observation_entity_binding_guard
  ON memory.observation_entity_binding;
CREATE TRIGGER observation_entity_binding_guard
BEFORE INSERT ON memory.observation_entity_binding
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_observation_binding();

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
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I', table_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'USING (owner_user_id = memory.current_actor_user_id()) '
      'WITH CHECK (owner_user_id = memory.current_actor_user_id())',
      table_name
    );
    EXECUTE format('REVOKE ALL ON memory.%I FROM PUBLIC', table_name);
    EXECUTE format('REVOKE ALL ON memory.%I FROM brains_app', table_name);
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
$rls$;

REVOKE ALL ON memory.predicate_registry_version FROM PUBLIC, brains_app;
REVOKE ALL ON memory.predicate_registry_seed FROM PUBLIC, brains_app;
REVOKE ALL ON memory.predicate_contract FROM PUBLIC, brains_app;
GRANT SELECT ON memory.predicate_registry_version, memory.predicate_contract
  TO brains_app;

REVOKE ALL ON FUNCTION memory.v5_sha256_valid(text) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_reason_codes_valid(jsonb, integer) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_source_spans_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_project_scope_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_literal_object_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_proposed_entity_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_candidate_features_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_relative_offset_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.v5_recurrence_valid(jsonb) FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_actor_active_evidence() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_resolution_plan() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_observation_contract() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_resolution_review() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_resolution_apply() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_observation_binding() FROM PUBLIC, brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_append_only() FROM PUBLIC, brains_app;

COMMIT;
