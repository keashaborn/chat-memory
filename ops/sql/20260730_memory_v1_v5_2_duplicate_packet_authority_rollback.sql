BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_supersession
    WHERE reason_code = 'duplicate_active_packet_reconciled'
  ) THEN
    RAISE EXCEPTION
      'duplicate authority rollback refused after durable reconciliation';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS
memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  uuid, uuid, uuid, uuid, text, text, text
);
DROP FUNCTION IF EXISTS
memory.plan_owner_v5_2_duplicate_packet_supersession_v1(uuid, uuid);

REVOKE SELECT ON memory.v5_2_local_packet_route_event
  FROM memory_v5_local_supersession_maintainer;
REVOKE SELECT ON memory.v5_local_packet_supersession
  FROM memory_v5_2_local_router_maintainer;

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (
    reason_code IN (
      'temporal_persistence_matrix_reextracted',
      'pet_identity_semantics_reextracted'
    )
  );

CREATE OR REPLACE FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(
  p_evidence_id uuid
)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
  WITH actor AS (
    SELECT memory.current_actor_user_id() AS owner_user_id
  ),
  candidates AS (
    SELECT
      packet.owner_user_id,
      packet.packet_id,
      packet.job_id,
      packet.evidence_id,
      packet.evidence_content_sha256
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN actor ON actor.owner_user_id = packet.owner_user_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id = packet.owner_user_id
     AND job.job_id = packet.job_id
     AND job.evidence_id = packet.evidence_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = packet.owner_user_id
     AND evidence.evidence_id = packet.evidence_id
    WHERE actor.owner_user_id IS NOT NULL
      AND p_evidence_id IS NOT NULL
      AND packet.evidence_id = p_evidence_id
      AND packet.normalized_packet->>'contract_version' =
          'memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version' =
          'memory_predicate_registry_v5_2'
      AND packet.provider_id = 'local_llama_cpp'
      AND packet.local_model_calls BETWEEN 0 AND 1
      AND packet.external_model_calls = 0
      AND packet.validator_packet_sha256 ~ '^[0-9a-f]{64}$'
      AND packet.packet_storage_sha256 = encode(public.digest(convert_to(
        packet.normalized_packet::text, 'UTF8'
      ), 'sha256'), 'hex')
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'entity_mentions') =
             'array'
        THEN jsonb_array_length(
          packet.normalized_packet->'entity_mentions'
        )
        ELSE -1
      END = packet.entity_mention_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'observations') =
             'array'
        THEN jsonb_array_length(packet.normalized_packet->'observations')
        ELSE -1
      END = packet.observation_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'comparison_hints') =
             'array'
        THEN jsonb_array_length(
          packet.normalized_packet->'comparison_hints'
        )
        ELSE -1
      END = packet.comparison_hint_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'deferrals') = 'array'
        THEN jsonb_array_length(packet.normalized_packet->'deferrals')
        ELSE -1
      END = packet.deferral_count
      AND job.status::text = 'review_required'
      AND job.route = 'relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status::text = 'active'
      AND evidence.content_sha256 = packet.evidence_content_sha256
  ),
  leaves AS (
    SELECT candidate.*
    FROM candidates AS candidate
    WHERE NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_event AS lineage
      JOIN memory.evidence_extraction_job AS successor_job
        ON successor_job.owner_user_id = lineage.owner_user_id
       AND successor_job.job_id = lineage.job_id
      WHERE lineage.owner_user_id = candidate.owner_user_id
        AND lineage.event_type = 'queued'
        AND lineage.details->>'prior_packet_id' =
            candidate.packet_id::text
        AND successor_job.evidence_id = candidate.evidence_id
        AND successor_job.evidence_content_sha256 =
            candidate.evidence_content_sha256
        AND successor_job.route = 'relational_extraction'
    )
  ),
  authority AS (
    SELECT
      count(*) AS leaf_count,
      (array_agg(packet_id ORDER BY packet_id))[1] AS packet_id
    FROM leaves
  )
  SELECT CASE WHEN leaf_count = 1 THEN packet_id END
  FROM authority;
$function$;

REVOKE EXECUTE ON FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  FROM memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  TO brains_app;

COMMIT;
