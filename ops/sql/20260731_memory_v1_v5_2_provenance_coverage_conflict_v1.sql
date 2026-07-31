BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';
SELECT pg_advisory_xact_lock(
  hashtextextended('memory_v5_2_provenance_coverage_conflict_v1',0)
);

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole(
       'memory_v5_2_reviewed_observation_stage_maintainer'
     ) IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)'
     ) IS NULL
     OR to_regclass('memory.predicate_contract') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL THEN
    RAISE EXCEPTION 'provenance/coverage/conflict prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.predicate_registry_compiler_extension_v1 (
  extension_id uuid PRIMARY KEY,
  registry_version text NOT NULL,
  compiler_version text NOT NULL,
  source_path text NOT NULL,
  artifact_sha256 text NOT NULL CHECK(
    artifact_sha256~'^[0-9a-f]{64}$'
  ),
  canonical_sha256 text NOT NULL CHECK(
    canonical_sha256~'^[0-9a-f]{64}$'
  ),
  contract_sha256 text NOT NULL CHECK(
    contract_sha256~'^[0-9a-f]{64}$'
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(registry_version,compiler_version),
  FOREIGN KEY(registry_version)
    REFERENCES memory.predicate_registry_version(registry_version)
);

CREATE OR REPLACE FUNCTION
memory.reject_predicate_registry_compiler_extension_v1_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  RAISE EXCEPTION 'predicate compiler extensions are append-only'
    USING ERRCODE='55000';
END
$function$;

DROP TRIGGER IF EXISTS predicate_registry_compiler_extension_v1_immutable
ON memory.predicate_registry_compiler_extension_v1;
CREATE TRIGGER predicate_registry_compiler_extension_v1_immutable
BEFORE UPDATE OR DELETE ON memory.predicate_registry_compiler_extension_v1
FOR EACH ROW EXECUTE FUNCTION
memory.reject_predicate_registry_compiler_extension_v1_mutation();

INSERT INTO memory.predicate_registry_compiler_extension_v1(
  extension_id,registry_version,compiler_version,source_path,
  artifact_sha256,canonical_sha256,contract_sha256
)
VALUES(
  '9a94477c-2f3d-5eb9-b640-2e4af208f974',
  'memory_predicate_registry_v5_2',
  'memory_v1_semantic_policy_compiler_v12',
  'specs/memory_v1_predicate_registry_v5_2_compiler_v12.json',
  'c244bc7c9727f5218c649dabd57e513053f73957316ad534a20b2f45ec719690',
  'd40e79057070aa6760af474dc8ca2695ad0024320afde562753e840356cf9f06',
  'f032afe95105ca30a937e1ead9ac67efe933e5d3a759eb0acd8bcfd5572ffd62'
)
ON CONFLICT DO NOTHING;

INSERT INTO memory.predicate(
  predicate,object_kind,cardinality,description,active
)
VALUES(
  'residence.care_setting','literal','one',
  'A directly reported residential care-setting category without inventing a specific facility.',
  true
)
ON CONFLICT(predicate) DO NOTHING;

INSERT INTO memory.predicate_registry_seed(
  registry_version,predicate,base_predicate_created
)
VALUES(
  'memory_predicate_registry_v5_2','residence.care_setting',true
)
ON CONFLICT(registry_version,predicate) DO NOTHING;

INSERT INTO memory.predicate_contract(
  predicate,registry_version,lifecycle,extraction_allowed,
  object_kind,cardinality,successor_predicates,contract,contract_sha256
)
VALUES(
  'residence.care_setting','memory_predicate_registry_v5_2','active',true,
  'literal','one','{}'::text[],
  $contract${
    "cardinality":"one_active",
    "description":"A directly reported residential care-setting category without inventing a specific facility.",
    "manual_review_rules":["third_party_care_setting"],
    "modalities":["asserted","reported_observation","uncertain","corrective"],
    "object_contract":"literal.care_setting",
    "predicate":"residence.care_setting",
    "projection_classes":["direct_claim","supportive_context"],
    "relation_semantics":"property",
    "sensitivity_floor":"high",
    "subject_entity_types":["person","self"],
    "surface_policies":["explicit_recall_only"],
    "temporal_semantics":["state_validity"]
  }$contract$::jsonb,
  'f032afe95105ca30a937e1ead9ac67efe933e5d3a759eb0acd8bcfd5572ffd62'
)
ON CONFLICT(predicate,registry_version) DO NOTHING;

CREATE OR REPLACE FUNCTION memory.v5_2_observation_semantic_slot_v1(
  p_predicate text,p_reason_codes jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path=pg_catalog
AS $function$
  SELECT CASE
    WHEN p_predicate='health.user_reported_observation'
      AND (
        p_reason_codes?'explicit_short_term_memory_duration'
        OR p_reason_codes?'reported_cognitive_symptom_duration_estimate'
      )
      THEN 'health.short_term_memory_duration'
    ELSE NULL
  END
$function$;

CREATE OR REPLACE FUNCTION
memory.owner_batch_possible_temporal_updates_v1(p_batch_id uuid)
RETURNS TABLE(
  semantic_slot text,
  current_observation_id uuid,
  prior_observation_id uuid,
  current_observation_sha256 text,
  prior_observation_sha256 text,
  subject_entity_id uuid,
  reason_code text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'temporal-update check requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_batch_id IS NULL THEN
    RAISE EXCEPTION 'batch ID is required' USING ERRCODE='22004';
  END IF;

  RETURN QUERY
  WITH current_observation AS (
    SELECT
      observation.observation_id,observation.observation_sha256,
      observation.predicate,observation.reason_codes,
      observation.object_literal,binding.subject_entity_id,
      temporal.instant_range,evidence.observed_at
    FROM memory.relational_stage_batch AS batch
    CROSS JOIN LATERAL jsonb_each_text(
      batch.result->'observation_ids'
    ) AS ids
    JOIN memory.observation AS observation
      ON observation.owner_user_id=batch.owner_user_id
     AND observation.observation_id=ids.value::uuid
     AND observation.evidence_id=batch.evidence_id
     AND observation.observation_ref=ids.key
    JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=observation.owner_user_id
     AND binding.observation_id=observation.observation_id
    JOIN memory.observation_temporal AS temporal
      ON temporal.owner_user_id=observation.owner_user_id
     AND temporal.observation_id=observation.observation_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
    WHERE batch.owner_user_id=actor
      AND batch.batch_id=p_batch_id
      AND jsonb_typeof(batch.result->'observation_ids')='object'
  )
  SELECT
    memory.v5_2_observation_semantic_slot_v1(
      current_value.predicate,current_value.reason_codes
    ),
    current_value.observation_id,prior.observation_id,
    current_value.observation_sha256,prior.observation_sha256,
    current_value.subject_entity_id,
    'overlapping_reported_value_change'::text
  FROM current_observation AS current_value
  JOIN memory.observation_entity_binding AS prior_binding
    ON prior_binding.owner_user_id=actor
   AND prior_binding.subject_entity_id=current_value.subject_entity_id
  JOIN memory.observation AS prior
    ON prior.owner_user_id=prior_binding.owner_user_id
   AND prior.observation_id=prior_binding.observation_id
   AND prior.observation_id<>current_value.observation_id
  JOIN memory.observation_temporal AS prior_temporal
    ON prior_temporal.owner_user_id=prior.owner_user_id
   AND prior_temporal.observation_id=prior.observation_id
  JOIN memory.evidence AS prior_evidence
    ON prior_evidence.owner_user_id=prior.owner_user_id
   AND prior_evidence.evidence_id=prior.evidence_id
  WHERE memory.v5_2_observation_semantic_slot_v1(
          current_value.predicate,current_value.reason_codes
        ) IS NOT NULL
    AND memory.v5_2_observation_semantic_slot_v1(
          prior.predicate,prior.reason_codes
        )=memory.v5_2_observation_semantic_slot_v1(
          current_value.predicate,current_value.reason_codes
        )
    AND prior_evidence.observed_at<current_value.observed_at
    AND prior.object_literal IS DISTINCT FROM current_value.object_literal
    AND prior_temporal.instant_range IS NOT NULL
    AND current_value.instant_range IS NOT NULL
    AND prior_temporal.instant_range&&current_value.instant_range
  ORDER BY prior_evidence.observed_at DESC,prior.observation_id;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.owner_batch_has_possible_temporal_update_v1(p_batch_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
  SELECT EXISTS(
    SELECT 1
    FROM memory.owner_batch_possible_temporal_updates_v1(p_batch_id)
  )
$function$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_reviewed_observation_stage_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  source_kind text,
  route_event_id uuid,
  atom_apply_id uuid,
  batch_id uuid,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  observation_state_sha256 text,
  observation_count integer,
  source_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'reviewed-observation plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.v5_2_atom_admission_apply AS atom
      ON atom.owner_user_id=route.owner_user_id
     AND atom.packet_id=route.packet_id
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=route.owner_user_id
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=atom.stage_projection_sha256
     AND batch.extractor IN (
       'memory_v1_v5_2_atom_stage_projection',
       'memory_v1_v5_2_atom_stage_projection_v2'
     )
    WHERE route.owner_user_id=actor
    UNION
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=route.owner_user_id
     AND request.request_id=route.request_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=request.owner_user_id
     AND batch.batch_id=(request.result->>'batch_id')::uuid
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=route.validator_packet_sha256
     AND batch.extractor='memory_v1_v5_2_local_packet_review'
    WHERE route.owner_user_id=actor
      AND route.blocking_code_count=0
  )
  SELECT source.source_kind,source.route_event_id,
    source.atom_apply_id,source.batch_id,source.stage_manifest_sha256,
    source.resolution_state_sha256,source.observation_state_sha256,
    source.observation_count,source.source_created_at
  FROM candidate
  CROSS JOIN LATERAL
    memory.v5_2_reviewed_observation_stage_source_v1(
      candidate.route_event_id,candidate.batch_id
    ) AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_stage_admission AS prior
    WHERE prior.owner_user_id=actor
      AND (
        prior.source_route_event_id=source.route_event_id
        OR (
          source.atom_apply_id IS NOT NULL
          AND prior.source_atom_apply_id=source.atom_apply_id
        )
      )
  )
    AND memory.owner_batch_has_unentailed_observation_v1(source.batch_id)
    AND NOT memory.owner_batch_has_possible_temporal_update_v1(
      source.batch_id
    )
  ORDER BY source.source_created_at,source.route_event_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_observation_conflict_v1(p_limit integer DEFAULT 20)
RETURNS TABLE(
  batch_id uuid,
  semantic_slot text,
  current_observation_id uuid,
  prior_observation_id uuid,
  current_observation_sha256 text,
  prior_observation_sha256 text,
  subject_entity_id uuid,
  reason_code text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'observation-conflict plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'observation-conflict limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT batch.batch_id,conflict.semantic_slot,
    conflict.current_observation_id,conflict.prior_observation_id,
    conflict.current_observation_sha256,conflict.prior_observation_sha256,
    conflict.subject_entity_id,conflict.reason_code
  FROM memory.relational_stage_batch AS batch
  CROSS JOIN LATERAL
    memory.owner_batch_possible_temporal_updates_v1(batch.batch_id)
      AS conflict
  WHERE batch.owner_user_id=actor
  ORDER BY batch.created_at,conflict.current_observation_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.v5_2_observation_semantic_slot_v1(text,jsonb)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.owner_batch_possible_temporal_updates_v1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.owner_batch_has_possible_temporal_update_v1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_observation_conflict_v1(integer)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;

REVOKE ALL ON memory.predicate_registry_compiler_extension_v1 FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.reject_predicate_registry_compiler_extension_v1_mutation()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.v5_2_observation_semantic_slot_v1(text,jsonb)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.owner_batch_possible_temporal_updates_v1(uuid)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.owner_batch_has_possible_temporal_update_v1(uuid)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_observation_conflict_v1(integer)
FROM PUBLIC;

GRANT SELECT ON memory.predicate_registry_compiler_extension_v1
TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION
  memory.v5_2_observation_semantic_slot_v1(text,jsonb)
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.owner_batch_possible_temporal_updates_v1(uuid)
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.owner_batch_has_possible_temporal_update_v1(uuid)
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_observation_conflict_v1(integer)
TO brains_app;

DO $postflight$
DECLARE
  extension_row memory.predicate_registry_compiler_extension_v1%ROWTYPE;
BEGIN
  SELECT * INTO STRICT extension_row
  FROM memory.predicate_registry_compiler_extension_v1
  WHERE extension_id='9a94477c-2f3d-5eb9-b640-2e4af208f974';
  IF extension_row.artifact_sha256<>
       'c244bc7c9727f5218c649dabd57e513053f73957316ad534a20b2f45ec719690'
     OR extension_row.canonical_sha256<>
       'd40e79057070aa6760af474dc8ca2695ad0024320afde562753e840356cf9f06'
     OR NOT EXISTS(
       SELECT 1 FROM memory.predicate_contract
       WHERE predicate='residence.care_setting'
         AND registry_version='memory_predicate_registry_v5_2'
         AND lifecycle='active' AND extraction_allowed
         AND object_kind='literal' AND cardinality='one'
         AND contract_sha256=
           'f032afe95105ca30a937e1ead9ac67efe933e5d3a759eb0acd8bcfd5572ffd62'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.owner_batch_possible_temporal_updates_v1(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.owner_batch_has_possible_temporal_update_v1(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_2_observation_conflict_v1(integer)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'provenance/coverage/conflict postflight failed';
  END IF;
END
$postflight$;

COMMIT;
