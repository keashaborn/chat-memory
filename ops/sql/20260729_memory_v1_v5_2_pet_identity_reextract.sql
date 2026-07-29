BEGIN;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_pet_identity_reextract_v1(
  p_prior_packet_id uuid
)
RETURNS TABLE(
  prior_packet_id uuid,
  prior_job_id uuid,
  evidence_id uuid,
  evidence_content_sha256 text,
  prior_packet_storage_sha256 text,
  prior_policy_compiler_sha256 text,
  prior_selector_version text,
  contextual_ordinal integer,
  reason_code text,
  next_selector_version text,
  next_policy_compiler_version text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  prior_compiler_sha constant text :=
    'f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419';
  next_selector constant text :=
    '20260729_v5_2_pet_identity_compiler_reextract_v1';
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'pet identity reextract plan requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_prior_packet_id IS NULL THEN
    RAISE EXCEPTION 'prior packet id is required'
      USING ERRCODE = '22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
    packet.evidence_content_sha256,
    packet.packet_storage_sha256,
    packet.policy_compiler_sha256,
    job.selector_version,
    (evidence.metadata->>'contextual_ordinal')::integer,
    CASE
      WHEN EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
          packet.normalized_packet->'entity_mentions'
        ) AS entity(value)
        WHERE entity.value->>'entity_type' = 'animal'
          AND entity.value->>'relationship_role' = 'pet:deceased'
      ) AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
          packet.normalized_packet->'observations'
        ) AS observation(value)
        WHERE observation.value->>'predicate' = 'life_event.died'
      )
      THEN 'unsupported_pet_deceased_role'
      ELSE 'ambiguous_pet_loss_identity_missing'
    END,
    next_selector,
    'memory_v1_semantic_policy_compiler_v8'::text
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id = packet.owner_user_id
   AND job.job_id = packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = packet.owner_user_id
   AND evidence.evidence_id = packet.evidence_id
  WHERE packet.owner_user_id = actor
    AND packet.packet_id = p_prior_packet_id
    AND packet.provider_id = 'local_llama_cpp'
    AND packet.provider_version = 'v1'
    AND packet.local_model_calls = 1
    AND packet.external_model_calls = 0
    AND packet.policy_compiler_sha256 = prior_compiler_sha
    AND packet.manual_review_required
    AND jsonb_typeof(packet.normalized_packet) = 'object'
    AND packet.packet_storage_sha256 = encode(public.digest(convert_to(
      packet.normalized_packet::text, 'UTF8'
    ), 'sha256'), 'hex')
    AND job.status = 'review_required'
    AND job.route = 'relational_extraction'
    AND job.selector_version = '20260729_v4_contextual_resplit'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status = 'active'
    AND evidence.content_sha256 = packet.evidence_content_sha256
    AND evidence.source_system = 'public.chat_log'
    AND evidence.metadata->>'span_origin' = 'contextual_split_v3'
    AND evidence.metadata->>'context_needed' = 'true'
    AND evidence.metadata->>'contextual_splitter_version' =
      'memory_v1_contextual_span_splitter_20260728_v3'
    AND evidence.metadata->>'contextual_parent_evidence_id' =
      'e7b0831a-9a15-48dd-97da-e97e9726a5e5'
    AND evidence.metadata->>'contextual_ordinal' IN ('0', '3')
    AND (
      (
        evidence.metadata->>'contextual_ordinal' = '0'
        AND packet.entity_mention_count = 1
        AND packet.observation_count = 1
        AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
            packet.normalized_packet->'entity_mentions'
          ) AS entity(value)
          WHERE entity.value->>'entity_type' = 'animal'
            AND entity.value->>'relationship_role' = 'pet:deceased'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
            packet.normalized_packet->'observations'
          ) AS observation(value)
          WHERE observation.value->>'predicate' = 'life_event.died'
        )
      )
      OR
      (
        evidence.metadata->>'contextual_ordinal' = '3'
        AND packet.entity_mention_count = 0
        AND packet.observation_count = 0
        AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
            packet.normalized_packet->'deferrals'
          ) AS deferral(value)
          WHERE deferral.value->>'reason_code' =
            'sensitive_manual_review'
        )
      )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id = actor
        AND stage.evidence_id = packet.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_supersession AS supersession
      WHERE supersession.owner_user_id = actor
        AND supersession.prior_packet_id = packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS next_job
      WHERE next_job.owner_user_id = actor
        AND next_job.evidence_id = packet.evidence_id
        AND next_job.selector_version = next_selector
    );
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
  p_operation_id uuid,
  p_new_job_id uuid,
  p_new_terminal_id uuid,
  p_prior_packet_id uuid,
  p_expected_content_sha256 text,
  p_expected_packet_storage_sha256 text,
  p_expected_reason_code text,
  p_selector_version text,
  p_next_policy_compiler_version text
)
RETURNS TABLE(
  job_id uuid,
  intake_terminal_id uuid,
  status text,
  apply_outcome text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replay_event memory.evidence_extraction_event%ROWTYPE;
  replay_job memory.evidence_extraction_job%ROWTYPE;
  replay_terminal memory.evidence_intake_terminal%ROWTYPE;
  fingerprint text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'pet identity reextract apply requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_new_job_id IS NULL
     OR p_new_terminal_id IS NULL
     OR p_prior_packet_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_reason_code NOT IN (
       'unsupported_pet_deceased_role',
       'ambiguous_pet_loss_identity_missing'
     )
     OR p_selector_version <>
       '20260729_v5_2_pet_identity_compiler_reextract_v1'
     OR p_next_policy_compiler_version <>
       'memory_v1_semantic_policy_compiler_v8' THEN
    RAISE EXCEPTION 'pet identity reextract inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws(
    '|',
    'memory_v1_v5_2_pet_identity_reextract_v1',
    actor::text,
    p_prior_packet_id::text,
    p_selector_version
  ), 0));

  SELECT event.* INTO replay_event
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id = actor
    AND event.operation_id = p_operation_id;
  IF FOUND THEN
    SELECT value.* INTO replay_job
    FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id = actor
      AND value.job_id = p_new_job_id;
    SELECT value.* INTO replay_terminal
    FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id = actor
      AND value.terminal_id = p_new_terminal_id;
    IF replay_job.job_id IS NULL
       OR replay_terminal.terminal_id IS NULL
       OR replay_event.job_id <> p_new_job_id
       OR replay_event.event_type <> 'queued'
       OR replay_event.from_status IS NOT NULL
       OR replay_event.to_status <> 'pending'
       OR replay_event.actor_type <> 'admin'
       OR replay_event.actor_ref IS DISTINCT FROM
          'local_v5_2_pet_identity_reextract_v1'
       OR replay_event.details->>'prior_packet_id'
          IS DISTINCT FROM p_prior_packet_id::text
       OR replay_event.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR replay_event.details->>'expected_packet_storage_sha256'
          IS DISTINCT FROM p_expected_packet_storage_sha256
       OR replay_event.details->>'reason_code'
          IS DISTINCT FROM p_expected_reason_code
       OR replay_job.intake_terminal_id <> p_new_terminal_id
       OR replay_job.evidence_id <> replay_terminal.evidence_id
       OR replay_job.selector_version <> p_selector_version
       OR replay_job.evidence_content_sha256 <>
          p_expected_content_sha256
       OR replay_job.route <> 'relational_extraction'
       OR replay_terminal.selector_version <> p_selector_version
       OR replay_terminal.evidence_content_sha256 <>
          p_expected_content_sha256
       OR replay_terminal.outcome <> 'dispatched'
       OR replay_terminal.reason_code <> 'eligible_dispatched' THEN
      RAISE EXCEPTION 'pet identity reextract replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
    SELECT replay_job.job_id, replay_terminal.terminal_id,
           replay_job.status::text, 'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_pet_identity_reextract_v1(
    p_prior_packet_id
  );
  IF NOT FOUND
     OR planned.evidence_content_sha256 <>
        p_expected_content_sha256
     OR planned.prior_packet_storage_sha256 <>
        p_expected_packet_storage_sha256
     OR planned.reason_code <> p_expected_reason_code
     OR planned.next_selector_version <> p_selector_version
     OR planned.next_policy_compiler_version <>
        p_next_policy_compiler_version THEN
    RAISE EXCEPTION 'pet identity reextract plan changed'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id = actor
      AND (
        value.job_id = p_new_job_id
        OR (
          value.evidence_id = planned.evidence_id
          AND value.selector_version = p_selector_version
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id = actor
      AND (
        value.terminal_id = p_new_terminal_id
        OR (
          value.evidence_id = planned.evidence_id
          AND value.selector_version = p_selector_version
        )
      )
  ) THEN
    RAISE EXCEPTION 'pet identity reextract target identifiers conflict'
      USING ERRCODE = '23514';
  END IF;

  fingerprint := encode(public.digest(convert_to(jsonb_build_object(
    'owner_user_id', actor,
    'evidence_id', planned.evidence_id,
    'selector_version', p_selector_version,
    'evidence_content_sha256', p_expected_content_sha256,
    'prior_packet_id', p_prior_packet_id,
    'prior_packet_storage_sha256', p_expected_packet_storage_sha256,
    'reason_code', p_expected_reason_code,
    'next_policy_compiler_version', p_next_policy_compiler_version
  )::text, 'UTF8'), 'sha256'), 'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,
    owner_user_id,
    evidence_id,
    selector_version,
    outcome,
    reason_code,
    evidence_content_sha256,
    decision_fingerprint,
    actor_user_id,
    invoked_by_role,
    details
  ) VALUES (
    p_new_terminal_id,
    actor,
    planned.evidence_id,
    p_selector_version,
    'dispatched',
    'eligible_dispatched',
    p_expected_content_sha256,
    fingerprint,
    actor,
    session_user,
    jsonb_build_object(
      'route', 'relational_extraction',
      'extraction_job_id', p_new_job_id,
      'reextract_reason', p_expected_reason_code,
      'prior_packet_id', p_prior_packet_id,
      'prior_packet_storage_sha256',
        p_expected_packet_storage_sha256,
      'prior_policy_compiler_sha256',
        planned.prior_policy_compiler_sha256,
      'next_policy_compiler_version',
        p_next_policy_compiler_version,
      'source_prose_copied', false
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,
    owner_user_id,
    evidence_id,
    intake_terminal_id,
    selector_version,
    evidence_content_sha256,
    route,
    intake_reason_code,
    status
  ) VALUES (
    p_new_job_id,
    actor,
    planned.evidence_id,
    p_new_terminal_id,
    p_selector_version,
    p_expected_content_sha256,
    'relational_extraction',
    'eligible_unprocessed',
    'pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,
    job_id,
    operation_id,
    event_type,
    from_status,
    to_status,
    actor_type,
    actor_ref,
    details
  ) VALUES (
    actor,
    p_new_job_id,
    p_operation_id,
    'queued',
    NULL,
    'pending',
    'admin',
    'local_v5_2_pet_identity_reextract_v1',
    jsonb_build_object(
      'prior_packet_id', p_prior_packet_id,
      'expected_content_sha256', p_expected_content_sha256,
      'expected_packet_storage_sha256',
        p_expected_packet_storage_sha256,
      'reason_code', p_expected_reason_code,
      'next_policy_compiler_version',
        p_next_policy_compiler_version,
      'source_prose_copied', false
    )
  );

  RETURN QUERY
  SELECT p_new_job_id, p_new_terminal_id, 'pending'::text,
         'applied'::text;
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_pet_identity_reextract_v1(uuid)
  OWNER TO memory_v5_local_reextract_maintainer;
ALTER FUNCTION memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
  uuid, uuid, uuid, uuid, text, text, text, text, text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_pet_identity_reextract_v1(uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
    uuid, uuid, uuid, uuid, text, text, text, text, text
  ) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_pet_identity_reextract_v1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
    uuid, uuid, uuid, uuid, text, text, text, text, text
  ) TO brains_app;

COMMENT ON FUNCTION
  memory.plan_owner_v5_2_pet_identity_reextract_v1(uuid)
IS 'Owner-scoped plan for append-only re-extraction of contextual pet packets affected by unsupported death-role or ambiguous-loss identity handling.';
COMMENT ON FUNCTION
  memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
    uuid, uuid, uuid, uuid, text, text, text, text, text
  )
IS 'Append-only, replay-safe enqueue for one hash-bound contextual pet identity re-extraction.';

COMMIT;
