BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
DECLARE
  exact_hash text;
  planner_hash text;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'worker contract repair requires sage';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_claim_projection_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.register_owner_v5_local_claim_projection_v1(uuid,uuid,uuid,uuid,text,text,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_claim_projection_packet_v5_1(uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'worker contract repair prerequisites are absent';
  END IF;

  exact_hash:=encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex');
  IF position(
       'AND packet.local_model_calls BETWEEN 0 AND 1'
       IN pg_get_functiondef(
         'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
       )
     )=0
     AND exact_hash<>
       '190e99d80d0b5d7ce6d742415384103b19d1dd68a20199a47b0ffcf772c7209b'
  THEN
    RAISE EXCEPTION 'exact packet planner hash drifted: %',exact_hash;
  END IF;

  planner_hash:=encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex');
  IF position(
       'memory.v5_local_claim_projection_terminal_v1'
       IN pg_get_functiondef(
         'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
       )
     )=0
     AND planner_hash<>
       '83849d6b99f6cd716a49e1acd151e8dd4d3c2d1a23c3b7bfa6c4df8c2aa213bf'
  THEN
    RAISE EXCEPTION 'claim projection planner hash drifted: %',planner_hash;
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.v5_local_claim_projection_terminal_v1 (
  terminal_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  packet_sha256 text NOT NULL
    CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
  owner_manifest_sha256 text NOT NULL
    CHECK (owner_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  semantic_key_sha256 text NOT NULL
    CHECK (semantic_key_sha256 ~ '^[0-9a-f]{64}$'),
  existing_claim_id uuid NOT NULL,
  policy_version text NOT NULL
    CHECK (policy_version='memory_v1_v5_local_claim_projection_policy_v1'),
  decision text NOT NULL CHECK (decision='already_materialized'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,assessment_id),
  UNIQUE(owner_user_id,observation_id),
  UNIQUE(owner_user_id,plan_id),
  FOREIGN KEY(owner_user_id,assessment_id)
    REFERENCES memory.v5_local_entailment_assessment(
      owner_user_id,assessment_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,observation_id)
    REFERENCES memory.observation(owner_user_id,observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,existing_claim_id)
    REFERENCES memory.claim(owner_user_id,claim_id)
    ON DELETE RESTRICT
);
ALTER TABLE memory.v5_local_claim_projection_terminal_v1 OWNER TO sage;
ALTER TABLE memory.v5_local_claim_projection_terminal_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_claim_projection_terminal_v1
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_local_claim_projection_terminal_v1;
CREATE POLICY owner_isolation
  ON memory.v5_local_claim_projection_terminal_v1
  TO memory_v5_local_projection_maintainer,memory_v5_writer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS v5_local_claim_projection_terminal_append_only_guard
  ON memory.v5_local_claim_projection_terminal_v1;
CREATE TRIGGER v5_local_claim_projection_terminal_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_local_claim_projection_terminal_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

GRANT SELECT,INSERT ON memory.v5_local_claim_projection_terminal_v1
  TO memory_v5_local_projection_maintainer,memory_v5_writer;
REVOKE ALL ON memory.v5_local_claim_projection_terminal_v1
  FROM PUBLIC,brains_app;
DROP POLICY IF EXISTS local_writer_read
  ON memory.v5_local_claim_projection_admission;
CREATE POLICY local_writer_read
  ON memory.v5_local_claim_projection_admission FOR SELECT
  TO memory_v5_writer
  USING (owner_user_id=memory.current_actor_user_id());
GRANT SELECT ON memory.v5_local_claim_projection_admission
  TO memory_v5_writer;

CREATE OR REPLACE FUNCTION memory.inspect_existing_claim_projection_v1(
  p_observation_id uuid,
  p_packet_text text
)
RETURNS TABLE(
  existing_claim_count integer,
  existing_claim_id uuid,
  semantic_key_sha256 text,
  packet_sha256 text,
  owner_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  computed_packet_sha256 text;
  computed_owner_manifest_sha256 text;
  matched_count integer;
  matched_claim_id uuid;
  matched_canonical_key text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'claim projection inspection requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL OR p_observation_id IS NULL
     OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'claim projection inspection context is invalid'
      USING ERRCODE='22023';
  END IF;
  BEGIN
    packet:=p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'claim projection packet is invalid JSON'
      USING ERRCODE='22023';
  END;
  computed_packet_sha256:=packet->>'packet_sha256';
  IF computed_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1
     OR jsonb_typeof(
          packet#>'{projections,0,observation_inputs}'
        )<>'array'
     OR jsonb_array_length(
          packet#>'{projections,0,observation_inputs}'
        )<>1
     OR packet#>>'{projections,0,observation_inputs,0,observation_id}'
          <>p_observation_id::text THEN
    RAISE EXCEPTION 'claim projection packet binding is invalid'
      USING ERRCODE='23514';
  END IF;
  computed_owner_manifest_sha256:=encode(public.digest(convert_to(
    '{"manifest_version":"memory_projection_owner_manifest_v5",'
    ||'"owner_user_id":"'||actor::text||'",'
    ||'"packet_sha256":"'||computed_packet_sha256||'"}',
    'UTF8'
  ),'sha256'),'hex');

  SELECT count(*),min(claim.claim_id::text)::uuid,
    min(claim.canonical_key)
  INTO matched_count,matched_claim_id,matched_canonical_key
  FROM memory.claim_observation AS claim_link
  JOIN memory.claim AS claim
    ON claim.owner_user_id=claim_link.owner_user_id
   AND claim.claim_id=claim_link.claim_id
  WHERE claim_link.owner_user_id=actor
    AND claim_link.observation_id=p_observation_id;
  IF matched_count NOT BETWEEN 0 AND 1 THEN
    RAISE EXCEPTION 'claim projection observation has multiple claims'
      USING ERRCODE='23514';
  END IF;
  IF matched_count=1
     AND matched_canonical_key !~ '^v5:[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'existing claim canonical key is outside V5'
      USING ERRCODE='23514';
  END IF;
  RETURN QUERY SELECT
    matched_count,matched_claim_id,
    CASE WHEN matched_count=1
      THEN substring(matched_canonical_key FROM 4)
      ELSE NULL
    END,
    computed_packet_sha256,computed_owner_manifest_sha256;
END
$function$;
ALTER FUNCTION memory.inspect_existing_claim_projection_v1(uuid,text)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.inspect_existing_claim_projection_v1(uuid,text) FROM PUBLIC,brains_app;

CREATE OR REPLACE FUNCTION memory.local_claim_projection_source_accepted_v1(
  p_assessment_id uuid,
  p_observation_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local claim source check requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM memory.v5_local_entailment_assessment AS assessment
    JOIN memory.observation AS observation
      ON observation.owner_user_id=assessment.owner_user_id
     AND observation.observation_id=assessment.observation_id
    WHERE assessment.owner_user_id=actor
      AND assessment.assessment_id=p_assessment_id
      AND assessment.observation_id=p_observation_id
      AND assessment.governed_decision='accepted'
      AND assessment.raw_decision='entailed'
      AND assessment.confidence='high'
      AND observation.predicate IN (
        'identity.name','pet.breed','pet.sex','relationship.has_pet',
        'relationship.parent_of','relationship.sibling_of'
      )
      AND observation.projection_class IN (
        'direct_claim','supportive_context'
      )
  );
END
$function$;
ALTER FUNCTION memory.local_claim_projection_source_accepted_v1(uuid,uuid)
  OWNER TO memory_v5_local_projection_maintainer;
REVOKE ALL ON FUNCTION
  memory.local_claim_projection_source_accepted_v1(uuid,uuid)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.local_claim_projection_source_accepted_v1(uuid,uuid)
  TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION
  memory.register_owner_v5_local_claim_projection_v1(
    uuid,uuid,uuid,uuid,text,text,text,text
  ) TO memory_v5_writer;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_local_claim_projection_v2(
  p_admission_id uuid,
  p_assessment_id uuid,
  p_observation_id uuid,
  p_plan_id uuid,
  p_packet_text text,
  p_expected_packet_sha256 text,
  p_expected_owner_manifest_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  plan_id uuid,
  decision text,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  terminal memory.v5_local_claim_projection_terminal_v1%ROWTYPE;
  inspection record;
  delegated record;
  accepted_source boolean;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local claim projection register requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_admission_id IS NULL OR p_assessment_id IS NULL
     OR p_observation_id IS NULL OR p_plan_id IS NULL
     OR p_packet_text IS NULL OR length(p_packet_text)>200000
     OR p_expected_packet_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_owner_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_version<>'memory_v1_v5_local_claim_projection_policy_v1'
     OR (p_packet_text::jsonb->>'packet_sha256')
          <>p_expected_packet_sha256 THEN
    RAISE EXCEPTION 'local claim projection inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_claim_projection',actor::text,
    p_observation_id::text
  ),0));

  SELECT value.* INTO terminal
  FROM memory.v5_local_claim_projection_terminal_v1 AS value
  WHERE value.owner_user_id=actor
    AND (value.terminal_id=p_admission_id
      OR value.assessment_id=p_assessment_id
      OR value.observation_id=p_observation_id
      OR value.plan_id=p_plan_id);
  IF FOUND THEN
    IF terminal.terminal_id<>p_admission_id
       OR terminal.assessment_id<>p_assessment_id
       OR terminal.observation_id<>p_observation_id
       OR terminal.plan_id<>p_plan_id
       OR terminal.packet_sha256<>p_expected_packet_sha256
       OR terminal.owner_manifest_sha256
            <>p_expected_owner_manifest_sha256
       OR terminal.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'local claim projection terminal replay conflicts'
        USING ERRCODE='23514';
    END IF;
    SELECT * INTO STRICT inspection
    FROM memory.inspect_existing_claim_projection_v1(
      p_observation_id,p_packet_text
    );
    IF inspection.existing_claim_count<>1
       OR inspection.existing_claim_id<>terminal.existing_claim_id
       OR inspection.semantic_key_sha256<>terminal.semantic_key_sha256
       OR inspection.packet_sha256<>terminal.packet_sha256
       OR inspection.owner_manifest_sha256<>terminal.owner_manifest_sha256 THEN
      RAISE EXCEPTION 'local claim projection terminal replay invariant failed'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT terminal.terminal_id,terminal.plan_id,
      terminal.decision,'replayed'::text,0;
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_claim_projection_admission AS admission
    WHERE admission.owner_user_id=actor
      AND (admission.admission_id=p_admission_id
        OR admission.assessment_id=p_assessment_id
        OR admission.observation_id=p_observation_id
        OR admission.plan_id=p_plan_id)
  ) THEN
    SELECT * INTO STRICT delegated
    FROM memory.register_owner_v5_local_claim_projection_v1(
      p_admission_id,p_assessment_id,p_observation_id,p_plan_id,
      p_packet_text,p_expected_packet_sha256,
      p_expected_owner_manifest_sha256,p_policy_version
    );
    RETURN QUERY SELECT delegated.admission_id,delegated.plan_id,
      delegated.decision,delegated.outcome,delegated.rows_written;
    RETURN;
  END IF;

  SELECT * INTO STRICT inspection
  FROM memory.inspect_existing_claim_projection_v1(
    p_observation_id,p_packet_text
  );
  IF inspection.packet_sha256<>p_expected_packet_sha256
     OR inspection.owner_manifest_sha256
          <>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'local claim projection packet binding failed'
      USING ERRCODE='23514';
  END IF;

  IF inspection.existing_claim_count=0 THEN
    SELECT * INTO STRICT delegated
    FROM memory.register_owner_v5_local_claim_projection_v1(
      p_admission_id,p_assessment_id,p_observation_id,p_plan_id,
      p_packet_text,p_expected_packet_sha256,
      p_expected_owner_manifest_sha256,p_policy_version
    );
    RETURN QUERY SELECT delegated.admission_id,delegated.plan_id,
      delegated.decision,delegated.outcome,delegated.rows_written;
    RETURN;
  END IF;

  SELECT memory.local_claim_projection_source_accepted_v1(
    p_assessment_id,p_observation_id
  ) INTO accepted_source;
  IF accepted_source IS NOT TRUE THEN
    RAISE EXCEPTION 'accepted local claim source not found'
      USING ERRCODE='P0002';
  END IF;

  INSERT INTO memory.v5_local_claim_projection_terminal_v1(
    terminal_id,owner_user_id,assessment_id,observation_id,plan_id,
    packet_sha256,owner_manifest_sha256,semantic_key_sha256,
    existing_claim_id,policy_version,decision
  ) VALUES (
    p_admission_id,actor,p_assessment_id,p_observation_id,p_plan_id,
    p_expected_packet_sha256,p_expected_owner_manifest_sha256,
    inspection.semantic_key_sha256,inspection.existing_claim_id,
    p_policy_version,'already_materialized'
  );
  RETURN QUERY SELECT p_admission_id,p_plan_id,
    'already_materialized'::text,'applied'::text,1;
END
$function$;
ALTER FUNCTION memory.register_owner_v5_local_claim_projection_v2(
  uuid,uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.register_owner_v5_local_claim_projection_v2(
    uuid,uuid,uuid,uuid,text,text,text,text
  ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.register_owner_v5_local_claim_projection_v2(
    uuid,uuid,uuid,uuid,text,text,text,text
  ) TO brains_app;

DO $packet_exact$
DECLARE
  definition text;
  patched text;
BEGIN
  definition:=pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  );
  IF position('AND packet.local_model_calls BETWEEN 0 AND 1' IN definition)>0
  THEN
    RETURN;
  END IF;
  IF (
    length(definition)
    -length(replace(definition,'AND packet.local_model_calls=1',''))
  )/length('AND packet.local_model_calls=1')<>1 THEN
    RAISE EXCEPTION 'exact packet local-call predicate is not singular';
  END IF;
  patched:=replace(
    definition,
    'AND packet.local_model_calls=1',
    'AND packet.local_model_calls BETWEEN 0 AND 1'
  );
  EXECUTE patched;
END
$packet_exact$;

DO $claim_planner$
DECLARE
  definition text;
  anchor text;
  replacement text;
BEGIN
  definition:=pg_get_functiondef(
    'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure
  );
  IF position(
       'memory.v5_local_claim_projection_terminal_v1' IN definition
     )>0 THEN
    RETURN;
  END IF;
  anchor:=$anchor$
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
$anchor$;
  replacement:=$replacement$
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_claim_projection_admission AS admission
      WHERE admission.owner_user_id=actor
        AND admission.assessment_id=assessment.assessment_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_claim_projection_terminal_v1 AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.assessment_id=assessment.assessment_id
    )
  ORDER BY assessment.created_at,assessment.assessment_id
$replacement$;
  IF position(anchor IN definition)=0
     OR position(anchor IN replace(definition,anchor,''))>0 THEN
    RAISE EXCEPTION 'claim planner admission anchor is not singular';
  END IF;
  EXECUTE replace(definition,anchor,replacement);
END
$claim_planner$;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_exact_packet_route_v1(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_exact_packet_route_v1(uuid) TO brains_app;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_claim_projection_v1(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_claim_projection_v1(integer) TO brains_app;

COMMIT;
