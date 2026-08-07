CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id uuid)
RETURNS TABLE(packet_id uuid, job_id uuid, evidence_id uuid, packet_storage_sha256 text, route text, reason_code text, routing_basis_sha256 text, source_deferral_reason_codes text[], entity_mention_count integer, observation_count integer, comparison_hint_count integer, deferral_count integer)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
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
      ARRAY(
        SELECT DISTINCT item->>'reason_code'
        FROM jsonb_array_elements(CASE
          WHEN jsonb_typeof(packet.normalized_packet->'deferrals')='array'
            THEN packet.normalized_packet->'deferrals'
          ELSE '[]'::jsonb
        END) AS item
        ORDER BY item->>'reason_code'
      )::text[] AS source_reason_codes,
      (
        packet.entity_mention_count=0
        AND packet.observation_count=0
        AND packet.comparison_hint_count=0
        AND packet.deferral_count>0
        AND NOT packet.manual_review_required
        AND NOT packet.normalized_packet @? '$.deferrals[*] ? (
          @.memory_shape != "none"
          || @.review_required != false
          || (
            @.reason_code != "structured_domain"
            && @.reason_code != "question_only"
            && @.reason_code != "transient_state"
            && @.reason_code != "insufficient_evidence"
          )
        )'
      ) AS terminal_eligible
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
      AND p_packet_id IS NOT NULL
      AND packet.packet_id=p_packet_id
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
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'entity_mentions')='array'
        THEN jsonb_array_length(packet.normalized_packet->'entity_mentions')
        ELSE -1
      END=packet.entity_mention_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'observations')='array'
        THEN jsonb_array_length(packet.normalized_packet->'observations')
        ELSE -1
      END=packet.observation_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'comparison_hints')='array'
        THEN jsonb_array_length(packet.normalized_packet->'comparison_hints')
        ELSE -1
      END=packet.comparison_hint_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'deferrals')='array'
        THEN jsonb_array_length(packet.normalized_packet->'deferrals')
        ELSE -1
      END=packet.deferral_count
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
  routed AS (
    SELECT
      value.*,
      CASE
        WHEN value.terminal_eligible THEN 'terminal_no_stage'
        WHEN value.entity_mention_count+value.observation_count
               +value.comparison_hint_count>=1
          THEN 'manual_review_artifact_ready'
      END AS selected_route
    FROM eligible AS value
  )
  SELECT
    value.packet_id,
    value.job_id,
    value.evidence_id,
    value.packet_storage_sha256,
    value.selected_route,
    CASE value.selected_route
      WHEN 'terminal_no_stage' THEN 'deferral_only_no_stage_v5_2'
      ELSE 'reviewable_relational_packet_v5_2'
    END,
    encode(public.digest(convert_to(jsonb_build_object(
      'packet_storage_sha256',value.packet_storage_sha256,
      'route',value.selected_route,
      'reason_codes',CASE
        WHEN value.selected_route='terminal_no_stage'
          THEN to_jsonb(value.source_reason_codes)
        ELSE '[]'::jsonb
      END,
      'entity_mention_count',value.entity_mention_count,
      'observation_count',value.observation_count,
      'comparison_hint_count',value.comparison_hint_count,
      'deferral_count',value.deferral_count
    )::text,'UTF8'),'sha256'),'hex'),
    CASE
      WHEN value.selected_route='terminal_no_stage'
        THEN value.source_reason_codes
      ELSE ARRAY[]::text[]
    END,
    value.entity_mention_count,
    value.observation_count,
    value.comparison_hint_count,
    value.deferral_count
  FROM routed AS value
  WHERE value.selected_route IS NOT NULL;
$function$;
ALTER FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) FROM brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) FROM memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid) TO memory_v5_2_local_router_maintainer;
