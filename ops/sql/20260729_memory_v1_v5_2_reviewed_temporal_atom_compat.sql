\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage'
     OR to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_atom_admission_v2(uuid)'
     ) IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL THEN
    RAISE EXCEPTION 'reviewed temporal atom compatibility prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.v5_2_temporal_review_registration (
  registration_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  route_event_id uuid NOT NULL,
  observation_ref text NOT NULL,
  packet_storage_sha256 text NOT NULL,
  stage_bundle_sha256 text NOT NULL,
  review_report_sha256 text NOT NULL,
  raw_temporal_sha256 text NOT NULL,
  reviewed_temporal jsonb NOT NULL,
  reviewed_temporal_sha256 text NOT NULL,
  review_reason_codes jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id,observation_ref),
  UNIQUE(owner_user_id,registration_id),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(route_event_id)
    REFERENCES memory.v5_2_local_packet_route_event(route_event_id)
    ON DELETE RESTRICT,
  CHECK (btrim(observation_ref) <> '' AND length(observation_ref) <= 100),
  CHECK (memory.v5_sha256_valid(packet_storage_sha256)),
  CHECK (memory.v5_sha256_valid(stage_bundle_sha256)),
  CHECK (memory.v5_sha256_valid(review_report_sha256)),
  CHECK (memory.v5_sha256_valid(raw_temporal_sha256)),
  CHECK (memory.v5_sha256_valid(reviewed_temporal_sha256)),
  CHECK (
    jsonb_typeof(reviewed_temporal) = 'object'
    AND pg_column_size(reviewed_temporal) <= 16384
  ),
  CHECK (
    memory.v5_reason_codes_valid(review_reason_codes,16)
    AND jsonb_array_length(review_reason_codes) >= 1
  )
);

ALTER TABLE memory.v5_2_temporal_review_registration
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_temporal_review_registration ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_temporal_review_registration FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_2_temporal_review_registration;
CREATE POLICY owner_isolation
  ON memory.v5_2_temporal_review_registration
  FOR ALL
  TO memory_v5_2_atom_admission_maintainer
  USING (
    owner_user_id = (
      SELECT memory.current_actor_user_id()
    )
  )
  WITH CHECK (
    owner_user_id = (
      SELECT memory.current_actor_user_id()
    )
  );

DROP TRIGGER IF EXISTS v5_2_temporal_review_registration_append_only_guard
  ON memory.v5_2_temporal_review_registration;
CREATE TRIGGER v5_2_temporal_review_registration_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_2_temporal_review_registration
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

REVOKE ALL ON memory.v5_2_temporal_review_registration
  FROM PUBLIC,brains_app;
GRANT SELECT,INSERT ON memory.v5_2_temporal_review_registration
  TO memory_v5_2_atom_admission_maintainer;

CREATE OR REPLACE FUNCTION
memory.review_owner_v5_2_temporal_projection_v1(
  p_operation_id uuid,
  p_registration_id uuid,
  p_packet_id uuid,
  p_evidence_id uuid,
  p_observation_ref text,
  p_expected_packet_storage_sha256 text,
  p_expected_stage_bundle_sha256 text,
  p_expected_review_report_sha256 text,
  p_expected_raw_temporal_sha256 text,
  p_reviewed_temporal jsonb,
  p_expected_reviewed_temporal_sha256 text,
  p_review_reason_codes jsonb
)
RETURNS TABLE(
  registration_id uuid,
  outcome text,
  reviewed_temporal_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  route memory.v5_2_local_packet_route_event%ROWTYPE;
  replay memory.v5_2_temporal_review_registration%ROWTYPE;
  raw_temporal jsonb;
  raw_temporal_sha text;
  reviewed_temporal_sha text;
  reviewed_without_codes jsonb;
  reviewed_normalized_to_raw jsonb;
  filtered_reason_codes jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_operation_id IS NULL
     OR p_registration_id IS NULL
     OR p_packet_id IS NULL
     OR p_evidence_id IS NULL
     OR btrim(COALESCE(p_observation_ref,'')) = ''
     OR NOT memory.v5_sha256_valid(p_expected_packet_storage_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_stage_bundle_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_review_report_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_raw_temporal_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_reviewed_temporal_sha256)
     OR NOT memory.v5_reason_codes_valid(p_review_reason_codes,16)
     OR jsonb_array_length(p_review_reason_codes) < 1 THEN
    RAISE EXCEPTION 'reviewed temporal registration inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws(
      '|',actor::text,'v5_2_temporal_review',
      p_packet_id::text,p_observation_ref
    ),
    0
  ));

  SELECT registration.* INTO replay
  FROM memory.v5_2_temporal_review_registration AS registration
  WHERE registration.owner_user_id = actor
    AND registration.operation_id = p_operation_id;
  IF FOUND THEN
    IF replay.registration_id <> p_registration_id
       OR replay.packet_id <> p_packet_id
       OR replay.evidence_id <> p_evidence_id
       OR replay.observation_ref <> p_observation_ref
       OR replay.packet_storage_sha256
            <> p_expected_packet_storage_sha256
       OR replay.stage_bundle_sha256
            <> p_expected_stage_bundle_sha256
       OR replay.review_report_sha256
            <> p_expected_review_report_sha256
       OR replay.raw_temporal_sha256
            <> p_expected_raw_temporal_sha256
       OR replay.reviewed_temporal_sha256
            <> p_expected_reviewed_temporal_sha256
       OR replay.reviewed_temporal IS DISTINCT FROM p_reviewed_temporal
       OR replay.review_reason_codes IS DISTINCT FROM p_review_reason_codes THEN
      RAISE EXCEPTION 'reviewed temporal registration replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      replay.registration_id,
      'replayed'::text,
      replay.reviewed_temporal_sha256;
    RETURN;
  END IF;

  SELECT source.* INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id = actor
    AND source.packet_id = p_packet_id
    AND source.evidence_id = p_evidence_id;
  IF NOT FOUND
     OR packet.packet_storage_sha256
          <> p_expected_packet_storage_sha256
     OR packet.packet_id IS DISTINCT FROM
        memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id) THEN
    RAISE EXCEPTION 'reviewed temporal packet binding is invalid'
      USING ERRCODE = '23514';
  END IF;

  SELECT source.* INTO route
  FROM memory.v5_2_local_packet_route_event AS source
  WHERE source.owner_user_id = actor
    AND source.packet_id = packet.packet_id
    AND source.evidence_id = packet.evidence_id
    AND source.route = 'manual_review_artifact_ready'
    AND source.reason_code = 'reviewable_relational_packet_v5_2';
  IF NOT FOUND
     OR route.packet_storage_sha256 <> packet.packet_storage_sha256
     OR route.stage_bundle_sha256 <> p_expected_stage_bundle_sha256
     OR route.review_report_sha256 <> p_expected_review_report_sha256 THEN
    RAISE EXCEPTION 'reviewed temporal route artifact binding is invalid'
      USING ERRCODE = '23514';
  END IF;

  SELECT observation.value->'temporal' INTO raw_temporal
  FROM jsonb_array_elements(packet.normalized_packet->'observations')
    AS observation(value)
  WHERE observation.value->>'observation_ref' = p_observation_ref;
  IF NOT FOUND OR raw_temporal IS NULL THEN
    RAISE EXCEPTION 'reviewed temporal observation is unavailable'
      USING ERRCODE = 'P0002';
  END IF;
  IF (
    SELECT count(*)
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      AS observation(value)
    WHERE observation.value->>'observation_ref' = p_observation_ref
  ) <> 1 THEN
    RAISE EXCEPTION 'reviewed temporal observation reference is ambiguous'
      USING ERRCODE = '23514';
  END IF;

  raw_temporal_sha := memory.v5_digest_text(
    memory.v5_canonical_json_text(raw_temporal)
  );
  reviewed_temporal_sha := memory.v5_digest_text(
    memory.v5_canonical_json_text(p_reviewed_temporal)
  );
  IF raw_temporal_sha <> p_expected_raw_temporal_sha256
     OR reviewed_temporal_sha <> p_expected_reviewed_temporal_sha256
     OR NOT memory.v5_jsonb_exact_keys(p_reviewed_temporal,ARRAY[
       'semantic','shape','basis','source_form','certainty','precision',
       'instant','calendar_range','instant_range','relative_offset',
       'recurrence','anchored_to_source_time',
       'normalization_policy_version','reason_codes'
     ])
     OR raw_temporal->>'basis' <> 'relative'
     OR raw_temporal->>'semantic' <> 'occurrence'
     OR raw_temporal->>'shape' <> 'instant'
     OR raw_temporal->>'source_form' <> 'relative'
     OR raw_temporal->>'anchored_to_source_time' <> 'false'
     OR raw_temporal->>'normalization_policy_version'
          <> 'memory_temporal_normalization_v5'
     OR raw_temporal->'relative_offset' IS DISTINCT FROM
        '{
          "direction":"past",
          "magnitude":1.0,
          "unit":"year",
          "approximate":true,
          "anchor_source":"evidence_observed_at"
        }'::jsonb
     OR NOT (raw_temporal->'reason_codes' @> '["reported_relative_year"]'::jsonb)
     OR p_reviewed_temporal->>'anchored_to_source_time' <> 'true'
     OR NOT (
       p_reviewed_temporal->'reason_codes'
       @> '[
         "relative_year_anchored_to_source_time",
         "review_last_year_relative_source_anchor"
       ]'::jsonb
     ) THEN
    RAISE EXCEPTION 'reviewed temporal transformation is not allowed'
      USING ERRCODE = '23514';
  END IF;

  SELECT COALESCE(jsonb_agg(code.value ORDER BY code.ordinality),'[]'::jsonb)
  INTO filtered_reason_codes
  FROM jsonb_array_elements(
    p_reviewed_temporal->'reason_codes'
  ) WITH ORDINALITY AS code(value,ordinality)
  WHERE code.value NOT IN (
    '"relative_year_anchored_to_source_time"'::jsonb,
    '"review_last_year_relative_source_anchor"'::jsonb
  );
  reviewed_without_codes := jsonb_set(
    p_reviewed_temporal,
    '{reason_codes}',
    filtered_reason_codes,
    false
  );
  reviewed_normalized_to_raw := jsonb_set(
    reviewed_without_codes,
    '{anchored_to_source_time}',
    'false'::jsonb,
    false
  );
  IF reviewed_normalized_to_raw IS DISTINCT FROM raw_temporal THEN
    RAISE EXCEPTION 'reviewed temporal changed more than the trusted anchor'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO memory.v5_2_temporal_review_registration(
    registration_id,
    owner_user_id,
    operation_id,
    packet_id,
    evidence_id,
    route_event_id,
    observation_ref,
    packet_storage_sha256,
    stage_bundle_sha256,
    review_report_sha256,
    raw_temporal_sha256,
    reviewed_temporal,
    reviewed_temporal_sha256,
    review_reason_codes
  ) VALUES (
    p_registration_id,
    actor,
    p_operation_id,
    packet.packet_id,
    packet.evidence_id,
    route.route_event_id,
    p_observation_ref,
    packet.packet_storage_sha256,
    route.stage_bundle_sha256,
    route.review_report_sha256,
    raw_temporal_sha,
    p_reviewed_temporal,
    reviewed_temporal_sha,
    p_review_reason_codes
  );

  RETURN QUERY SELECT
    p_registration_id,
    'applied'::text,
    reviewed_temporal_sha;
END
$function$;

ALTER FUNCTION memory.review_owner_v5_2_temporal_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,jsonb,text,jsonb
) OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION memory.review_owner_v5_2_temporal_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,jsonb,text,jsonb
) FROM PUBLIC,brains_app,memory_v5_2_atom_admission_maintainer;
GRANT EXECUTE ON FUNCTION memory.review_owner_v5_2_temporal_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,jsonb,text,jsonb
) TO brains_app;

CREATE OR REPLACE FUNCTION
memory.apply_owner_v5_2_temporal_reviews_v1(
  p_owner_user_id uuid,
  p_packet_id uuid,
  p_packet jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  actor uuid;
  observations jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_owner_user_id IS DISTINCT FROM actor
     OR p_packet_id IS NULL
     OR jsonb_typeof(p_packet) <> 'object'
     OR jsonb_typeof(p_packet->'observations') <> 'array' THEN
    RAISE EXCEPTION 'reviewed temporal projection inputs are invalid'
      USING ERRCODE = '42501';
  END IF;

  SELECT COALESCE(
    jsonb_agg(
      CASE
        WHEN registration.registration_id IS NULL
          THEN observation.value
        ELSE jsonb_set(
          observation.value,
          '{temporal}',
          registration.reviewed_temporal,
          false
        )
      END
      ORDER BY observation.ordinality
    ),
    '[]'::jsonb
  ) INTO observations
  FROM jsonb_array_elements(p_packet->'observations')
    WITH ORDINALITY AS observation(value,ordinality)
  LEFT JOIN memory.v5_2_temporal_review_registration AS registration
    ON registration.owner_user_id = actor
   AND registration.packet_id = p_packet_id
   AND registration.observation_ref =
        observation.value->>'observation_ref';

  IF jsonb_array_length(observations)
       <> jsonb_array_length(p_packet->'observations') THEN
    RAISE EXCEPTION 'reviewed temporal projection changed observation count'
      USING ERRCODE = '23514';
  END IF;
  RETURN jsonb_set(p_packet,'{observations}',observations,false);
END
$function$;

ALTER FUNCTION memory.apply_owner_v5_2_temporal_reviews_v1(
  uuid,uuid,jsonb
) OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION memory.apply_owner_v5_2_temporal_reviews_v1(
  uuid,uuid,jsonb
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_owner_v5_2_temporal_reviews_v1(
  uuid,uuid,jsonb
) TO memory_v5_2_atom_admission_maintainer;

DO $preserve_and_patch_planner$
DECLARE
  definition text;
  backup_definition text;
  marker text := E'\n  WITH\n';
  marker_position integer;
  prefix text;
  suffix text;
BEGIN
  IF to_regprocedure(
    'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)'
  ) IS NULL THEN
    SELECT pg_get_functiondef(
      'memory.plan_owner_v5_2_atom_admission_v2(uuid)'::regprocedure
    ) INTO backup_definition;
    backup_definition := replace(
      backup_definition,
      'memory.plan_owner_v5_2_atom_admission_v2',
      'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1'
    );
    EXECUTE backup_definition;
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)'
      ::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  IF position('  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;'
       IN definition) = 0
     OR position(marker IN definition) = 0 THEN
    RAISE EXCEPTION 'V2 atom planner differs from temporal patch contract';
  END IF;
  definition := replace(
    definition,
    '  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;',
    E'  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;\n'
      '  reviewed_packet jsonb;'
  );
  marker_position := position(marker IN definition);
  prefix := substring(definition FROM 1 FOR marker_position - 1);
  suffix := substring(definition FROM marker_position);
  IF position('packet.normalized_packet' IN suffix) = 0 THEN
    RAISE EXCEPTION 'V2 atom planner suffix has no packet source';
  END IF;
  suffix := replace(
    suffix,
    'packet.normalized_packet',
    'reviewed_packet'
  );
  definition := prefix
    || E'\n  reviewed_packet := '
    || 'memory.apply_owner_v5_2_temporal_reviews_v1('
    || 'actor,packet.packet_id,packet.normalized_packet);'
    || suffix;
  EXECUTE definition;
END
$preserve_and_patch_planner$;

ALTER FUNCTION
  memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_atom_admission_v2_before_temporal_review_v1(uuid)
  FROM PUBLIC,brains_app;

COMMENT ON TABLE memory.v5_2_temporal_review_registration IS
  'Append-only owner-scoped registration of an exact trusted temporal transformation already present in a hash-bound reviewed stage artifact.';
COMMENT ON FUNCTION memory.review_owner_v5_2_temporal_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,jsonb,text,jsonb
) IS
  'Registers only an exact reviewed relative-year source anchor for the authenticated owner. It never changes the immutable provider packet.';
COMMENT ON FUNCTION memory.apply_owner_v5_2_temporal_reviews_v1(
  uuid,uuid,jsonb
) IS
  'Projects append-only reviewed temporal metadata into atom planning while preserving the immutable provider packet.';
COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid) IS
  'Builds the V5.2 atom plan from the immutable packet plus any exact append-only reviewed temporal registrations.';

COMMIT;
