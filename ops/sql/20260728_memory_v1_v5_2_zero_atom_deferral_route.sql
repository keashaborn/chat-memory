BEGIN;

ALTER TABLE memory.v5_2_local_packet_route_event
  DROP CONSTRAINT v5_2_local_packet_route_event_check,
  DROP CONSTRAINT v5_2_local_packet_route_event_reason_code_check;

ALTER TABLE memory.v5_2_local_packet_route_event
  ADD CONSTRAINT v5_2_local_packet_route_event_reason_code_check
  CHECK (reason_code = ANY (ARRAY[
    'deferral_only_no_stage_v5_2'::text,
    'deferral_only_review_unresolved_v5_2'::text,
    'reviewable_relational_packet_v5_2'::text
  ])),
  ADD CONSTRAINT v5_2_local_packet_route_event_check
  CHECK (
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_no_stage_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 4
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence'
      ]::text[]
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    )
    OR
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_review_unresolved_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 5
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence','entity_resolution_unresolved'
      ]::text[]
      AND 'entity_resolution_unresolved'=ANY(source_deferral_reason_codes)
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    )
    OR
    (
      route='manual_review_artifact_ready'
      AND reason_code='reviewable_relational_packet_v5_2'
      AND entity_mention_count+observation_count+comparison_hint_count>=1
      AND cardinality(source_deferral_reason_codes)=0
      AND review_id IS NOT NULL
      AND request_id IS NOT NULL
      AND review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND review_report_sha256 ~ '^[0-9a-f]{64}$'
      AND stage_bundle_sha256 ~ '^[0-9a-f]{64}$'
      AND repository_commit ~ '^[0-9a-f]{40}$'
      AND auto_link_count BETWEEN 0 AND 32
      AND manual_review_count BETWEEN 0 AND 32
      AND deferred_resolution_count BETWEEN 0 AND 32
      AND rejected_count BETWEEN 0 AND 32
      AND blocking_code_count BETWEEN 0 AND 32
      AND auto_link_count+manual_review_count
            +deferred_resolution_count+rejected_count=entity_mention_count
    )
  );

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_zero_atom_deferral_route_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  packet_storage_sha256 text,
  route text,
  reason_code text,
  routing_basis_sha256 text,
  source_deferral_reason_codes text[],
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
  WITH actor AS (
    SELECT memory.current_actor_user_id() AS owner_user_id
  ),
  eligible AS (
    SELECT
      packet.packet_id,
      packet.job_id,
      packet.evidence_id,
      packet.packet_storage_sha256,
      packet.entity_mention_count,
      packet.observation_count,
      packet.comparison_hint_count,
      packet.deferral_count,
      packet.manual_review_required,
      packet.created_at,
      ARRAY(
        SELECT DISTINCT item->>'reason_code'
        FROM jsonb_array_elements(packet.normalized_packet->'deferrals') AS item
        ORDER BY item->>'reason_code'
      )::text[] AS source_reason_codes
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN actor ON actor.owner_user_id=packet.owner_user_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id
     AND job.job_id=packet.job_id
     AND job.evidence_id=packet.evidence_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    WHERE actor.owner_user_id IS NOT NULL
      AND p_limit BETWEEN 1 AND 25
      AND packet.packet_id=
          memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id)
      AND packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5_2'
      AND packet.provider_id='local_llama_cpp'
      AND packet.local_model_calls BETWEEN 0 AND 1
      AND packet.external_model_calls=0
      AND packet.validator_packet_sha256 ~ '^[0-9a-f]{64}$'
      AND packet.packet_storage_sha256=encode(public.digest(convert_to(
        packet.normalized_packet::text,'UTF8'
      ),'sha256'),'hex')
      AND jsonb_typeof(packet.normalized_packet->'entity_mentions')='array'
      AND jsonb_array_length(packet.normalized_packet->'entity_mentions')
            =packet.entity_mention_count
      AND jsonb_typeof(packet.normalized_packet->'observations')='array'
      AND jsonb_array_length(packet.normalized_packet->'observations')
            =packet.observation_count
      AND jsonb_typeof(packet.normalized_packet->'comparison_hints')='array'
      AND jsonb_array_length(packet.normalized_packet->'comparison_hints')
            =packet.comparison_hint_count
      AND jsonb_typeof(packet.normalized_packet->'deferrals')='array'
      AND jsonb_array_length(packet.normalized_packet->'deferrals')
            =packet.deferral_count
      AND packet.entity_mention_count=0
      AND packet.observation_count=0
      AND packet.comparison_hint_count=0
      AND packet.deferral_count BETWEEN 1 AND 32
      AND (
        (
          NOT packet.manual_review_required
          AND NOT packet.normalized_packet @? '$.deferrals[*] ? (
            @.review_required != false
            || (
              @.reason_code != "structured_domain"
              && @.reason_code != "question_only"
              && @.reason_code != "transient_state"
              && @.reason_code != "insufficient_evidence"
            )
          )'
        )
        OR
        (
          packet.manual_review_required
          AND packet.normalized_packet
                @? '$.deferrals[*] ? (@.review_required == true)'
          AND packet.normalized_packet
                @? '$.deferrals[*] ? (
                  @.reason_code == "entity_resolution_unresolved"
                )'
          AND NOT packet.normalized_packet @? '$.deferrals[*] ? (
            @.reason_code != "structured_domain"
            && @.reason_code != "question_only"
            && @.reason_code != "transient_state"
            && @.reason_code != "insufficient_evidence"
            && @.reason_code != "entity_resolution_unresolved"
          )'
        )
      )
      AND job.status::text='review_required'
      AND job.route='relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status::text='active'
      AND evidence.content_sha256=packet.evidence_content_sha256
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_2_local_packet_route_event AS prior
        WHERE prior.owner_user_id=packet.owner_user_id
          AND prior.packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_local_packet_disposition AS prior
        WHERE prior.owner_user_id=packet.owner_user_id
          AND prior.packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.relational_stage_batch AS stage
        WHERE stage.owner_user_id=packet.owner_user_id
          AND stage.evidence_id=packet.evidence_id
      )
  ),
  classified AS (
    SELECT
      value.*,
      CASE
        WHEN value.manual_review_required
          THEN 'deferral_only_review_unresolved_v5_2'
        ELSE 'deferral_only_no_stage_v5_2'
      END AS selected_reason
    FROM eligible AS value
    ORDER BY value.created_at,value.packet_id
    LIMIT p_limit
  )
  SELECT
    value.packet_id,
    value.job_id,
    value.evidence_id,
    value.packet_storage_sha256,
    'terminal_no_stage'::text,
    value.selected_reason,
    encode(public.digest(convert_to(jsonb_build_object(
      'packet_storage_sha256',value.packet_storage_sha256,
      'route','terminal_no_stage',
      'reason_code',value.selected_reason,
      'reason_codes',to_jsonb(value.source_reason_codes),
      'entity_mention_count',value.entity_mention_count,
      'observation_count',value.observation_count,
      'comparison_hint_count',value.comparison_hint_count,
      'deferral_count',value.deferral_count
    )::text,'UTF8'),'sha256'),'hex'),
    value.source_reason_codes,
    value.entity_mention_count,
    value.observation_count,
    value.comparison_hint_count,
    value.deferral_count
  FROM classified AS value;
$function$;

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
  p_operation_id uuid,
  p_route_event_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_reason_code text,
  p_expected_routing_basis_sha256 text,
  p_source_deferral_reason_codes text[]
)
RETURNS TABLE(
  route_event_id uuid,
  packet_id uuid,
  route text,
  reason_code text,
  apply_outcome text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  target record;
  replayed memory.v5_2_local_packet_route_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 zero-atom route requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_route_event_id IS NULL OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_routing_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_reason_code NOT IN (
       'deferral_only_no_stage_v5_2',
       'deferral_only_review_unresolved_v5_2'
     )
     OR cardinality(p_source_deferral_reason_codes) NOT BETWEEN 1 AND 5 THEN
    RAISE EXCEPTION 'V5.2 zero-atom route inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_packet_route',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_2_local_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'terminal_no_stage'
       OR replayed.reason_code<>p_expected_reason_code
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.source_deferral_reason_codes
            <>p_source_deferral_reason_codes THEN
      RAISE EXCEPTION 'V5.2 zero-atom route replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.route_event_id,replayed.packet_id,
      replayed.route,replayed.reason_code,'replayed'::text;
    RETURN;
  END IF;

  SELECT planned.* INTO target
  FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(25) AS planned
  WHERE planned.packet_id=p_packet_id;
  IF NOT FOUND
     OR target.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR target.reason_code<>p_expected_reason_code
     OR target.routing_basis_sha256<>p_expected_routing_basis_sha256
     OR target.source_deferral_reason_codes
          <>p_source_deferral_reason_codes THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 zero-atom routing'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_local_packet_route_event(
    route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    route,reason_code,routing_basis_sha256,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,entity_mention_count,
    observation_count,comparison_hint_count,deferral_count,
    source_deferral_reason_codes
  )
  SELECT
    p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
    packet.evidence_id,'terminal_no_stage',p_expected_reason_code,
    p_expected_routing_basis_sha256,packet.evidence_content_sha256,
    packet.validator_packet_sha256,packet.packet_storage_sha256,
    packet.entity_mention_count,packet.observation_count,
    packet.comparison_hint_count,packet.deferral_count,
    p_source_deferral_reason_codes
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_packet_id;

  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'terminal_no_stage'::text,p_expected_reason_code,'applied'::text;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
  uuid,uuid,uuid,text,text,text,text[]
) OWNER TO memory_v5_2_local_router_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
    uuid,uuid,uuid,text,text,text,text[]
  )
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
    uuid,uuid,uuid,text,text,text,text[]
  )
  TO brains_app;

COMMENT ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)
IS 'Owner-scoped planner for authoritative V5.2 packets containing deferrals but no relational atoms.';
COMMENT ON FUNCTION
  memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
    uuid,uuid,uuid,text,text,text,text[]
  )
IS 'Append-only owner-scoped V5.2 finalizer for zero-atom terminal or unresolved-review deferrals.';

COMMIT;
