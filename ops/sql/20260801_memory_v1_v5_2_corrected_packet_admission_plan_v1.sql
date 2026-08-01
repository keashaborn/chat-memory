BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure(
          'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'
        ) IS NULL
     OR to_regprocedure(
          'memory.v5_2_observation_semantic_slot_v1(text,jsonb)'
        ) IS NULL
     OR to_regprocedure('memory.normalize_entity_name_v5(text)') IS NULL
     OR to_regclass('memory.v5_local_packet_supersession') IS NULL
     OR to_regclass('memory.entity_resolution_apply') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL THEN
    RAISE EXCEPTION 'corrected-packet admission planner prerequisites absent';
  END IF;
  IF to_regclass(
       'memory.v5_2_corrected_packet_admission_proposal_v1'
     ) IS NOT NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)'
     ) IS NOT NULL THEN
    RAISE EXCEPTION 'corrected-packet admission planner already installed';
  END IF;
END
$preflight$;

CREATE TABLE memory.v5_2_corrected_packet_admission_proposal_v1 (
  proposal_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  prior_packet_id uuid NOT NULL,
  packet_storage_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(packet_storage_sha256)),
  prior_packet_storage_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(prior_packet_storage_sha256)),
  plan jsonb NOT NULL CHECK (
    jsonb_typeof(plan)='object' AND pg_column_size(plan)<=131072
  ),
  plan_sha256 text NOT NULL CHECK (memory.v5_sha256_valid(plan_sha256)),
  entity_reuse_count smallint NOT NULL CHECK (
    entity_reuse_count BETWEEN 0 AND 24
  ),
  admit_new_count smallint NOT NULL CHECK (admit_new_count BETWEEN 0 AND 32),
  repair_existing_count smallint NOT NULL CHECK (
    repair_existing_count BETWEEN 0 AND 32
  ),
  reuse_existing_count smallint NOT NULL CHECK (
    reuse_existing_count BETWEEN 0 AND 32
  ),
  temporal_hold_count smallint NOT NULL CHECK (
    temporal_hold_count BETWEEN 0 AND 32
  ),
  policy_version text NOT NULL CHECK (
    policy_version='memory_v1_v5_2_corrected_packet_admission_policy_v1'
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,proposal_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
  UNIQUE(owner_user_id,plan_sha256),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,prior_packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id) ON DELETE RESTRICT
);
ALTER TABLE memory.v5_2_corrected_packet_admission_proposal_v1
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_corrected_packet_admission_proposal_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_corrected_packet_admission_proposal_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation
  ON memory.v5_2_corrected_packet_admission_proposal_v1
  TO memory_v5_2_atom_admission_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
CREATE TRIGGER v5_2_corrected_packet_proposal_append_only
BEFORE UPDATE OR DELETE
ON memory.v5_2_corrected_packet_admission_proposal_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

DO $policies$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'evidence','evidence_extraction_packet_v5_local',
    'v5_local_packet_supersession','v5_2_local_packet_route_event',
    'entity','entity_mention','entity_resolution_plan',
    'entity_resolution_apply','entity_alias_observation',
    'observation','observation_entity_binding','observation_temporal',
    'claim_observation','relational_stage_batch'
  ] LOOP
    EXECUTE format(
      'CREATE POLICY v5_2_correction_plan_read ON memory.%I '
      'FOR SELECT TO memory_v5_2_atom_admission_maintainer '
      'USING (owner_user_id=memory.current_actor_user_id())',
      table_name
    );
  END LOOP;
END
$policies$;

GRANT USAGE ON SCHEMA memory TO memory_v5_2_atom_admission_maintainer;
GRANT SELECT ON
  memory.evidence,
  memory.evidence_extraction_packet_v5_local,
  memory.v5_local_packet_supersession,
  memory.v5_2_local_packet_route_event,
  memory.entity,
  memory.entity_mention,
  memory.entity_resolution_plan,
  memory.entity_resolution_apply,
  memory.entity_alias_observation,
  memory.observation,
  memory.observation_entity_binding,
  memory.observation_temporal,
  memory.claim_observation,
  memory.relational_stage_batch
TO memory_v5_2_atom_admission_maintainer;
GRANT SELECT,INSERT ON
  memory.v5_2_corrected_packet_admission_proposal_v1
TO memory_v5_2_atom_admission_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.current_actor_user_id(),
  memory.v5_digest_text(text),
  memory.v5_canonical_json_text(jsonb),
  memory.v5_sha256_valid(text),
  memory.normalize_entity_name_v5(text),
  memory.authoritative_owner_v5_2_packet_id_v1(uuid),
  memory.v5_2_observation_semantic_slot_v1(text,jsonb)
TO memory_v5_2_atom_admission_maintainer;

CREATE OR REPLACE FUNCTION
memory.v5_2_packet_observation_range_v1(p_observation jsonb)
RETURNS tstzrange
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path=''
AS $function$
  SELECT CASE
    WHEN jsonb_typeof(p_observation#>'{temporal,instant_range}')='object'
      AND nullif(p_observation#>>'{temporal,instant_range,lower}','')
        IS NOT NULL
    THEN tstzrange(
      (p_observation#>>'{temporal,instant_range,lower}')::timestamptz,
      nullif(p_observation#>>'{temporal,instant_range,upper}','')::timestamptz,
      coalesce(nullif(p_observation#>>'{temporal,instant_range,bounds}',''),'[)')
    )
    ELSE NULL
  END
$function$;

CREATE OR REPLACE FUNCTION
memory.v5_2_corrected_packet_plan_sha_v1(p_plan jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path=''
AS $function$
  SELECT memory.v5_digest_text(memory.v5_canonical_json_text(
    p_plan-'plan_sha256'
  ))
$function$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_corrected_packet_admission_v1(p_packet_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  prior_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  supersession memory.v5_local_packet_supersession%ROWTYPE;
  source_evidence memory.evidence%ROWTYPE;
  entity_decisions jsonb;
  observation_decisions jsonb;
  counts jsonb;
  plan_value jsonb;
  plan_sha text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'corrected-packet plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL OR p_packet_id IS NULL THEN
    RAISE EXCEPTION 'owner actor and packet ID are required'
      USING ERRCODE='22004';
  END IF;

  SELECT value.* INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor AND value.packet_id=p_packet_id;
  IF NOT FOUND
     OR packet.packet_id<>memory.authoritative_owner_v5_2_packet_id_v1(
       packet.evidence_id
     )
     OR jsonb_typeof(packet.normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'observations')<>'array'
     OR NOT EXISTS (
       SELECT 1 FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=packet.packet_id
         AND route.evidence_id=packet.evidence_id
         AND route.route='manual_review_artifact_ready'
     )
     OR EXISTS (
       SELECT 1 FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=packet.packet_id
         AND route.evidence_id=packet.evidence_id
         AND route.route='terminal_no_stage'
     ) THEN
    RAISE EXCEPTION 'owner-authoritative corrected packet is unavailable'
      USING ERRCODE='P0002';
  END IF;

  SELECT value.* INTO supersession
  FROM memory.v5_local_packet_supersession AS value
  WHERE value.owner_user_id=actor
    AND value.replacement_packet_id=packet.packet_id
    AND value.evidence_id=packet.evidence_id
  ORDER BY value.created_at DESC
  LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'corrected packet has no append-only predecessor'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO STRICT prior_packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor
    AND value.packet_id=supersession.prior_packet_id;
  SELECT value.* INTO STRICT source_evidence
  FROM memory.evidence AS value
  WHERE value.owner_user_id=actor
    AND value.evidence_id=packet.evidence_id;

  WITH source_entity AS (
    SELECT item,item->>'entity_ref' AS entity_ref,
      item->>'entity_type' AS entity_type,item->>'name_text' AS name_text,
      item->>'relationship_role' AS relationship_role,ordinality::integer
    FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions')
      WITH ORDINALITY AS source(item,ordinality)
  ), prior_match AS (
    SELECT source.*,
      coalesce(candidate.match_count,0)::integer AS match_count,
      candidate.mention_id,candidate.resolution_id,candidate.entity_id
    FROM source_entity AS source
    LEFT JOIN LATERAL (
      SELECT count(DISTINCT applied.applied_entity_id) AS match_count,
        (array_agg(mention.mention_id ORDER BY applied.applied_at DESC))[1]
          AS mention_id,
        (array_agg(applied.resolution_id ORDER BY applied.applied_at DESC))[1]
          AS resolution_id,
        (array_agg(applied.applied_entity_id
          ORDER BY applied.applied_at DESC))[1] AS entity_id
      FROM memory.entity_mention AS mention
      JOIN memory.entity_resolution_plan AS resolution
        ON resolution.owner_user_id=mention.owner_user_id
       AND resolution.mention_id=mention.mention_id
      JOIN memory.entity_resolution_apply AS applied
        ON applied.owner_user_id=resolution.owner_user_id
       AND applied.resolution_id=resolution.resolution_id
      WHERE mention.owner_user_id=actor
        AND mention.evidence_id=packet.evidence_id
        AND mention.entity_ref=source.entity_ref
        AND mention.entity_type=source.entity_type
        AND coalesce(mention.relationship_role,'')=
            coalesce(source.relationship_role,'')
        AND (
          (mention.name_text IS NULL AND source.name_text IS NULL)
          OR memory.normalize_entity_name_v5(mention.name_text)=
             memory.normalize_entity_name_v5(source.name_text)
        )
    ) AS candidate ON true
  )
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'entity_ref',entity_ref,
    'entity_type',entity_type,
    'name_sha256',CASE WHEN name_text IS NULL THEN NULL
      ELSE memory.v5_digest_text(name_text) END,
    'relationship_role',relationship_role,
    'source_atom_sha256',memory.v5_digest_text(
      memory.v5_canonical_json_text(item)
    ),
    'match_count',match_count,
    'prior_mention_id',mention_id,
    'prior_resolution_id',resolution_id,
    'target_entity_id',entity_id,
    'disposition',CASE
      WHEN match_count=1 THEN 'reuse_existing_resolution'
      WHEN match_count=0 THEN 'manual_entity_resolution_required'
      ELSE 'manual_ambiguous_entity_resolution'
    END
  ) ORDER BY ordinality),'[]'::jsonb)
  INTO entity_decisions
  FROM prior_match;

  WITH source_observation AS (
    SELECT item,item->>'observation_ref' AS observation_ref,
      item->>'subject_entity_ref' AS subject_entity_ref,
      item->>'predicate' AS predicate,item->'object' AS object_literal,
      item->'source_spans' AS source_spans,item->'reason_codes' AS reason_codes,
      item->>'polarity' AS polarity,item->>'modality' AS modality,
      item->>'sensitivity' AS sensitivity,ordinality::integer
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      WITH ORDINALITY AS source(item,ordinality)
  ), resolved AS (
    SELECT source.*,(decision->>'target_entity_id')::uuid AS target_entity_id,
      decision->>'disposition' AS entity_disposition
    FROM source_observation AS source
    LEFT JOIN LATERAL (
      SELECT value AS decision
      FROM jsonb_array_elements(entity_decisions) AS value
      WHERE value->>'entity_ref'=source.subject_entity_ref
      LIMIT 1
    ) AS entity_value ON true
  ), classified AS (
    SELECT resolved.*,
      coalesce(exact.match_count,0)::integer AS exact_match_count,
      exact.observation_id AS prior_observation_id,
      exact.observation_sha256 AS prior_observation_sha256,
      exact.source_spans AS prior_source_spans,
      coalesce(exact.claim_link_count,0)::integer AS claim_link_count,
      coalesce(conflict.conflict_count,0)::integer AS conflict_count,
      coalesce(conflict.conflicts,'[]'::jsonb) AS conflicts
    FROM resolved
    LEFT JOIN LATERAL (
      SELECT count(*) AS match_count,
        (array_agg(observation.observation_id
          ORDER BY observation.created_at DESC))[1] AS observation_id,
        (array_agg(observation.observation_sha256
          ORDER BY observation.created_at DESC))[1] AS observation_sha256,
        (array_agg(observation.source_spans
          ORDER BY observation.created_at DESC))[1] AS source_spans,
        count(DISTINCT linked.claim_id) AS claim_link_count
      FROM memory.observation AS observation
      JOIN memory.observation_entity_binding AS binding
        ON binding.owner_user_id=observation.owner_user_id
       AND binding.observation_id=observation.observation_id
      LEFT JOIN memory.claim_observation AS linked
        ON linked.owner_user_id=observation.owner_user_id
       AND linked.observation_id=observation.observation_id
      WHERE observation.owner_user_id=actor
        AND observation.evidence_id=packet.evidence_id
        AND binding.subject_entity_id=resolved.target_entity_id
        AND observation.predicate=resolved.predicate
        AND observation.object_literal=resolved.object_literal
        AND observation.polarity::text=resolved.polarity
        AND observation.modality::text=resolved.modality
    ) AS exact ON true
    LEFT JOIN LATERAL (
      SELECT count(*) AS conflict_count,
        coalesce(jsonb_agg(jsonb_build_object(
          'prior_observation_id',prior.observation_id,
          'prior_observation_sha256',prior.observation_sha256,
          'reason_code','overlapping_reported_value_change'
        ) ORDER BY prior_evidence.observed_at DESC),'[]'::jsonb) AS conflicts
      FROM memory.observation AS prior
      JOIN memory.observation_entity_binding AS prior_binding
        ON prior_binding.owner_user_id=prior.owner_user_id
       AND prior_binding.observation_id=prior.observation_id
      JOIN memory.observation_temporal AS prior_temporal
        ON prior_temporal.owner_user_id=prior.owner_user_id
       AND prior_temporal.observation_id=prior.observation_id
      JOIN memory.evidence AS prior_evidence
        ON prior_evidence.owner_user_id=prior.owner_user_id
       AND prior_evidence.evidence_id=prior.evidence_id
      WHERE prior.owner_user_id=actor
        AND prior_binding.subject_entity_id=resolved.target_entity_id
        AND memory.v5_2_observation_semantic_slot_v1(
              resolved.predicate,resolved.reason_codes
            ) IS NOT NULL
        AND memory.v5_2_observation_semantic_slot_v1(
              prior.predicate,prior.reason_codes
            )=memory.v5_2_observation_semantic_slot_v1(
              resolved.predicate,resolved.reason_codes
            )
        AND prior.object_literal IS DISTINCT FROM resolved.object_literal
        AND prior_evidence.observed_at<source_evidence.observed_at
        AND prior_temporal.instant_range IS NOT NULL
        AND memory.v5_2_packet_observation_range_v1(resolved.item)
              IS NOT NULL
        AND prior_temporal.instant_range &&
            memory.v5_2_packet_observation_range_v1(resolved.item)
    ) AS conflict ON true
  )
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'observation_ref',observation_ref,
    'predicate',predicate,
    'source_atom_sha256',memory.v5_digest_text(
      memory.v5_canonical_json_text(item)
    ),
    'target_entity_id',target_entity_id,
    'entity_disposition',entity_disposition,
    'exact_match_count',exact_match_count,
    'prior_observation_id',prior_observation_id,
    'prior_observation_sha256',prior_observation_sha256,
    'prior_source_spans_sha256',CASE WHEN prior_source_spans IS NULL THEN NULL
      ELSE memory.v5_digest_text(
        memory.v5_canonical_json_text(prior_source_spans)
      ) END,
    'corrected_source_spans_sha256',memory.v5_digest_text(
      memory.v5_canonical_json_text(source_spans)
    ),
    'claim_link_count',claim_link_count,
    'storage_action',CASE
      WHEN target_entity_id IS NULL THEN 'blocked_entity_resolution'
      WHEN exact_match_count>1 THEN 'manual_duplicate_observation'
      WHEN exact_match_count=1
       AND prior_source_spans IS DISTINCT FROM source_spans
        THEN 'repair_existing_provenance'
      WHEN exact_match_count=1 THEN 'reuse_existing_observation'
      ELSE 'admit_new_observation'
    END,
    'governance_action',CASE
      WHEN conflict_count>0 THEN 'hold_possible_temporal_update'
      WHEN sensitivity='high' THEN 'manual_sensitive_review'
      ELSE 'controlled_review_eligible'
    END,
    'conflict_count',conflict_count,
    'conflicts',conflicts
  ) ORDER BY ordinality),'[]'::jsonb)
  INTO observation_decisions
  FROM classified;

  SELECT jsonb_build_object(
    'entity_reuse_count',count(*) FILTER(
      WHERE value->>'disposition'='reuse_existing_resolution'
    ),
    'entity_manual_count',count(*) FILTER(
      WHERE value->>'disposition'<>'reuse_existing_resolution'
    )
  ) INTO counts
  FROM jsonb_array_elements(entity_decisions) AS value;
  SELECT counts || jsonb_build_object(
    'observation_count',count(*),
    'admit_new_count',count(*) FILTER(
      WHERE value->>'storage_action'='admit_new_observation'
    ),
    'repair_existing_count',count(*) FILTER(
      WHERE value->>'storage_action'='repair_existing_provenance'
    ),
    'reuse_existing_count',count(*) FILTER(
      WHERE value->>'storage_action'='reuse_existing_observation'
    ),
    'blocked_count',count(*) FILTER(
      WHERE value->>'storage_action' LIKE 'blocked%'
         OR value->>'storage_action' LIKE 'manual%'
    ),
    'temporal_hold_count',count(*) FILTER(
      WHERE value->>'governance_action'='hold_possible_temporal_update'
    ),
    'manual_sensitive_count',count(*) FILTER(
      WHERE value->>'governance_action'='manual_sensitive_review'
    ),
    'claim_rebind_count',coalesce(sum(
      (value->>'claim_link_count')::integer
    ) FILTER(
      WHERE value->>'storage_action'='repair_existing_provenance'
    ),0)
  ) INTO counts
  FROM jsonb_array_elements(observation_decisions) AS value;

  plan_value:=jsonb_build_object(
    'contract_version','memory_v1_v5_2_corrected_packet_admission_plan_v1',
    'policy_version','memory_v1_v5_2_corrected_packet_admission_policy_v1',
    'owner_user_id',actor::text,
    'packet_id',packet.packet_id::text,
    'evidence_id',packet.evidence_id::text,
    'prior_packet_id',prior_packet.packet_id::text,
    'packet_storage_sha256',packet.packet_storage_sha256,
    'prior_packet_storage_sha256',prior_packet.packet_storage_sha256,
    'packet_supersession_id',supersession.supersession_id::text,
    'packet_supersession_reason',supersession.reason_code,
    'entity_decisions',entity_decisions,
    'observation_decisions',observation_decisions,
    'source_deferral_reason_codes',coalesce((
      SELECT jsonb_agg(value->>'reason_code' ORDER BY ordinality)
      FROM jsonb_array_elements(packet.normalized_packet->'deferrals')
        WITH ORDINALITY AS source(value,ordinality)
    ),'[]'::jsonb),
    'counts',counts,
    'plan_sha256',''
  );
  plan_sha:=memory.v5_2_corrected_packet_plan_sha_v1(plan_value);
  RETURN jsonb_set(plan_value,'{plan_sha256}',to_jsonb(plan_sha),false);
END
$function$;

CREATE OR REPLACE FUNCTION
memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
  p_proposal_id uuid,
  p_operation_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_plan_sha256 text
)
RETURNS TABLE(
  proposal_id uuid,
  outcome text,
  plan_sha256 text,
  storage_change_count integer,
  temporal_hold_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  plan_value jsonb;
  existing memory.v5_2_corrected_packet_admission_proposal_v1%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'corrected-packet proposal requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL OR p_proposal_id IS NULL OR p_operation_id IS NULL
     OR p_packet_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_packet_storage_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_plan_sha256) THEN
    RAISE EXCEPTION 'corrected-packet proposal inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    actor::text,'corrected_packet_admission',p_packet_id::text
  ),0));
  plan_value:=memory.plan_owner_v5_2_corrected_packet_admission_v1(
    p_packet_id
  );
  IF plan_value->>'packet_storage_sha256'
       <>p_expected_packet_storage_sha256
     OR plan_value->>'plan_sha256'<>p_expected_plan_sha256 THEN
    RAISE EXCEPTION 'corrected-packet proposal hash mismatch'
      USING ERRCODE='23514';
  END IF;
  SELECT value.* INTO existing
  FROM memory.v5_2_corrected_packet_admission_proposal_v1 AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF existing.proposal_id<>p_proposal_id
       OR existing.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR existing.plan_sha256<>p_expected_plan_sha256
       OR existing.plan IS DISTINCT FROM plan_value THEN
      RAISE EXCEPTION 'corrected-packet proposal replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing.proposal_id,'replayed',existing.plan_sha256,
      (existing.admit_new_count+existing.repair_existing_count)::integer,
      existing.temporal_hold_count::integer;
    RETURN;
  END IF;
  INSERT INTO memory.v5_2_corrected_packet_admission_proposal_v1(
    proposal_id,owner_user_id,operation_id,packet_id,evidence_id,
    prior_packet_id,packet_storage_sha256,prior_packet_storage_sha256,
    plan,plan_sha256,entity_reuse_count,admit_new_count,
    repair_existing_count,reuse_existing_count,temporal_hold_count,
    policy_version
  ) VALUES (
    p_proposal_id,actor,p_operation_id,p_packet_id,
    (plan_value->>'evidence_id')::uuid,
    (plan_value->>'prior_packet_id')::uuid,
    p_expected_packet_storage_sha256,
    plan_value->>'prior_packet_storage_sha256',plan_value,
    p_expected_plan_sha256,
    (plan_value#>>'{counts,entity_reuse_count}')::smallint,
    (plan_value#>>'{counts,admit_new_count}')::smallint,
    (plan_value#>>'{counts,repair_existing_count}')::smallint,
    (plan_value#>>'{counts,reuse_existing_count}')::smallint,
    (plan_value#>>'{counts,temporal_hold_count}')::smallint,
    'memory_v1_v5_2_corrected_packet_admission_policy_v1'
  );
  RETURN QUERY SELECT p_proposal_id,'applied',p_expected_plan_sha256,
    (plan_value#>>'{counts,admit_new_count}')::integer+
      (plan_value#>>'{counts,repair_existing_count}')::integer,
    (plan_value#>>'{counts,temporal_hold_count}')::integer;
END
$function$;

ALTER FUNCTION memory.v5_2_packet_observation_range_v1(jsonb)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.v5_2_corrected_packet_plan_sha_v1(jsonb)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION
memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
  uuid,uuid,uuid,text,text
) OWNER TO memory_v5_2_atom_admission_maintainer;

REVOKE ALL ON FUNCTION
  memory.v5_2_packet_observation_range_v1(jsonb),
  memory.v5_2_corrected_packet_plan_sha_v1(jsonb),
  memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid),
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    uuid,uuid,uuid,text,text
  )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid),
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    uuid,uuid,uuid,text,text
  )
TO brains_app;

COMMENT ON TABLE memory.v5_2_corrected_packet_admission_proposal_v1 IS
  'Append-only owner-scoped plans for reconciling an authoritative corrected packet with already-staged observations before any durable correction or admission.';
COMMENT ON FUNCTION
memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid) IS
  'Classifies corrected packet atoms as entity reuse, new observation admission, provenance repair, exact reuse, or temporal hold without writing governed data.';

COMMIT;
