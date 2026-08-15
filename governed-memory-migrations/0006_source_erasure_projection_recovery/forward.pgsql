SET ROLE governed_memory_owner;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
-- Terminal projection failures are no longer an owner-wide deletion lock.
CREATE OR REPLACE FUNCTION memory_private.lease_projection_jobs(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer,
  p_runtime_contract_sha256 text
)
RETURNS TABLE(
  owner_user_id uuid,
  outbox_id uuid,
  claim_id uuid,
  revision_id uuid,
  revision_number integer,
  operation_id uuid,
  operation text,
  sequence_number integer,
  point_id uuid,
  collection_alias text,
  revision_sha256 text,
  selection_binding_sha256 text,
  predicate_catalog_sha256 text,
  projection_contract_sha256 text,
  dimensions integer,
  embedding_model text,
  renderer_sha256 text,
  projection_manifest_sha256 text,
  retrieval_text text,
  retrieval_text_sha256 text,
  embedding_input_sha256 text,
  source_sha256 text,
  lifecycle_state text,
  is_current boolean,
  predicate text,
  epistemic_state text,
  sensitivity text,
  domains text[],
  intents text[],
  surface text,
  requires_explicit boolean,
  projectable boolean,
  valid_from timestamptz,
  valid_to timestamptz,
  claim_updated_at timestamptz,
  state_sha256 text,
  semantic_key_sha256 text,
  claim_identity_sha256 text,
  subject_entity_key text,
  subject_entity_type text,
  subject_display_name text,
  object_kind text,
  object_entity_key text,
  object_entity_type text,
  object_display_name text,
  object_literal jsonb,
  lease_token uuid,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  new_lease_token uuid;
  new_lease_expires_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF COALESCE(p_worker_id, '') !~ '^[a-z][a-z0-9_:/.-]{0,127}$'
     OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 5 AND 300
     OR p_runtime_contract_sha256 IS DISTINCT FROM
          'd5ef651ccf1607f00c93da9f2e88219a366c178dbc2d5a015e7911af3cb05ba8'
  THEN
    RAISE EXCEPTION 'invalid projection lease input' USING ERRCODE = '22023';
  END IF;

  UPDATE memory.projection_outbox AS expired_outbox
  SET state = CASE
        WHEN expired_outbox.embedding_request_sha256 IS NOT NULL
          THEN 'failed_terminal'
        WHEN expired_outbox.attempt_count >= expired_outbox.max_attempts
          THEN 'failed_terminal'
        ELSE 'retryable'
      END,
      available_at = pg_catalog.clock_timestamp(),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL,
      last_error_code = CASE
        WHEN expired_outbox.embedding_request_sha256 IS NOT NULL
          THEN 'embedding_dispatch_outcome_unknown'
        ELSE 'lease_expired'
      END,
      completion_lease_token = CASE
        WHEN expired_outbox.embedding_request_sha256 IS NOT NULL
          THEN expired_outbox.lease_token
        ELSE NULL::uuid
      END,
      completion_outcome = CASE
        WHEN expired_outbox.embedding_request_sha256 IS NOT NULL
          THEN 'failed_terminal'
        ELSE NULL::text
      END,
      updated_at = pg_catalog.clock_timestamp()
  WHERE expired_outbox.state = 'claimed'
    AND expired_outbox.lease_expires_at <= pg_catalog.clock_timestamp()
    AND (
      NOT memory_private.owner_source_erasure_active(
        expired_outbox.owner_user_id
      )
      OR EXISTS (
        SELECT 1
        FROM memory.source_erasure_claim AS source_claim
        JOIN memory.source_erasure_operation AS source_operation
          ON source_operation.owner_user_id = source_claim.owner_user_id
         AND source_operation.operation_id = source_claim.operation_id
        WHERE source_claim.owner_user_id = expired_outbox.owner_user_id
          AND source_claim.claim_id = expired_outbox.claim_id
          AND source_operation.state <> 'completed'
      )
    );

  WITH superseded AS (
    UPDATE memory.projection_outbox AS outbox
    SET state = 'superseded',
        last_error_code = 'stale_projection_sequence',
        completion_lease_token = NULL, completion_outcome = NULL,
        updated_at = pg_catalog.transaction_timestamp()
    WHERE outbox.state IN ('pending', 'retryable')
      AND outbox.embedding_request_sha256 IS NULL
      AND (
        NOT memory_private.owner_source_erasure_active(outbox.owner_user_id)
        OR EXISTS (
          SELECT 1
          FROM memory.source_erasure_claim AS source_claim
          JOIN memory.source_erasure_operation AS source_operation
            ON source_operation.owner_user_id = source_claim.owner_user_id
           AND source_operation.operation_id = source_claim.operation_id
          WHERE source_claim.owner_user_id = outbox.owner_user_id
            AND source_claim.claim_id = outbox.claim_id
            AND source_operation.state <> 'completed'
        )
      )
      AND EXISTS (
        SELECT 1
        FROM memory.claim AS claim
        WHERE claim.owner_user_id = outbox.owner_user_id
          AND claim.claim_id = outbox.claim_id
          AND outbox.sequence_number < claim.projection_sequence
      )
    RETURNING outbox.owner_user_id, outbox.outbox_id,
      outbox.operation_id, outbox.projection_manifest_sha256,
      outbox.sequence_number
  ), receipts AS (
    SELECT superseded.*,
      pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        superseded.owner_user_id::text || '|'
          || superseded.outbox_id::text || '|'
          || superseded.operation_id::text || '|'
          || superseded.sequence_number::text
          || '|stale_projection_sequence',
        'UTF8'
      )), 'hex') AS receipt_sha256
    FROM superseded
  )
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, object_type, object_id,
    transition_code, prior_state_sha256, new_state_sha256,
    reason_code, event_sha256
  )
  SELECT receipt.owner_user_id, receipt.operation_id,
    'system', 'projection_outbox', receipt.outbox_id,
    'projection_superseded', receipt.projection_manifest_sha256,
    receipt.receipt_sha256, 'stale_projection_sequence',
    receipt.receipt_sha256
  FROM receipts AS receipt;

  FOR candidate IN
    SELECT outbox.*, revision.revision_number,
           revision.predicate_catalog_sha256,
           revision.retrieval_text, revision.retrieval_text_sha256,
           revision.source_sha256, revision.predicate,
           revision.epistemic_state, revision.sensitivity,
           revision.domains, revision.intents, revision.surface,
           revision.requires_explicit, revision.projectable,
           revision.valid_from, revision.valid_to,
           claim.lifecycle_state, claim.current_revision_id,
           claim.current_state_sha256, claim.semantic_key_sha256,
           claim.claim_identity_sha256,
           revision.subject_entity_key, revision.subject_entity_type,
           revision.subject_display_name,
           revision.object_kind, revision.object_entity_key,
           revision.object_entity_type, revision.object_display_name,
           revision.object_literal,
           claim.updated_at AS claim_updated_at
    FROM memory.projection_outbox AS outbox
    JOIN memory.claim_revision AS revision
      ON revision.owner_user_id = outbox.owner_user_id
     AND revision.claim_id = outbox.claim_id
     AND revision.revision_id = outbox.revision_id
    JOIN memory.claim AS claim
      ON claim.owner_user_id = outbox.owner_user_id
     AND claim.claim_id = outbox.claim_id
    WHERE outbox.state IN ('pending', 'retryable')
      AND outbox.available_at <= pg_catalog.clock_timestamp()
      AND outbox.attempt_count < outbox.max_attempts
      AND (
        NOT memory_private.owner_source_erasure_active(outbox.owner_user_id)
        OR (
          outbox.operation = 'delete'
          AND EXISTS (
            SELECT 1
            FROM memory.source_erasure_claim AS source_claim
            JOIN memory.source_erasure_operation AS source_operation
              ON source_operation.owner_user_id = source_claim.owner_user_id
             AND source_operation.operation_id = source_claim.operation_id
            WHERE source_claim.owner_user_id = outbox.owner_user_id
              AND source_claim.claim_id = outbox.claim_id
              AND source_claim.claim_delete_operation_id
                    = outbox.operation_id
              AND source_operation.state IN (
                'fenced', 'claim_deletion_pending'
              )
          )
        )
      )
      AND claim.projection_sequence = outbox.sequence_number
      AND claim.current_revision_id = outbox.revision_id
      AND claim.current_state_sha256 = memory_private.claim_state_sha256(
        claim.owner_user_id, claim.claim_id, claim.semantic_key_sha256,
        claim.claim_identity_sha256, claim.lifecycle_state, true,
        claim.current_revision_id, claim.current_revision_number,
        revision.revision_sha256, claim.projection_sequence
      )
      AND outbox.projection_manifest_sha256
            = memory_private.projection_manifest_sha256(
                outbox.owner_user_id, outbox.claim_id, outbox.revision_id,
                outbox.operation_id, outbox.operation,
                outbox.sequence_number, outbox.revision_sha256,
                outbox.selection_binding_sha256,
                revision.retrieval_text_sha256,
                revision.retrieval_text_sha256
              )
      AND (
        (outbox.operation = 'upsert'
          AND claim.lifecycle_state = 'active'
          AND revision.projectable)
        OR
        (outbox.operation = 'delete'
          AND claim.lifecycle_state IN (
            'correction_pending', 'retracted', 'deletion_pending'
          ))
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.projection_outbox AS earlier
        WHERE earlier.owner_user_id = outbox.owner_user_id
          AND earlier.claim_id = outbox.claim_id
          AND earlier.sequence_number < outbox.sequence_number
          AND (
            earlier.state IN ('pending', 'retryable', 'claimed')
            OR (
              earlier.state = 'failed_terminal'
              AND earlier.embedding_request_sha256 IS NOT NULL
              AND NOT (
                outbox.operation = 'delete'
                AND EXISTS (
                  SELECT 1
                  FROM memory.source_erasure_claim AS erasure_claim
                  JOIN memory.source_erasure_operation AS erasure_operation
                    ON erasure_operation.owner_user_id
                         = erasure_claim.owner_user_id
                   AND erasure_operation.operation_id
                         = erasure_claim.operation_id
                  WHERE erasure_claim.owner_user_id = outbox.owner_user_id
                    AND erasure_claim.claim_id = outbox.claim_id
                    AND erasure_claim.claim_delete_operation_id
                          = outbox.operation_id
                    AND erasure_operation.state IN (
                      'fenced', 'claim_deletion_pending'
                    )
                )
              )
            )
          )
      )
    ORDER BY outbox.available_at, outbox.created_at, outbox.outbox_id
    FOR UPDATE OF outbox SKIP LOCKED
    LIMIT p_limit
  LOOP
    new_lease_token := pg_catalog.gen_random_uuid();
    new_lease_expires_at := pg_catalog.clock_timestamp()
      + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE memory.projection_outbox
    SET state = 'claimed', attempt_count = attempt_count + 1,
        lease_token = new_lease_token, claimed_by = p_worker_id,
        claimed_at = pg_catalog.clock_timestamp(),
        lease_expires_at = new_lease_expires_at,
        last_error_code = NULL, completion_lease_token = NULL,
        completion_outcome = NULL, updated_at = pg_catalog.clock_timestamp()
    WHERE projection_outbox.owner_user_id = candidate.owner_user_id
      AND projection_outbox.outbox_id = candidate.outbox_id;
    RETURN QUERY SELECT candidate.owner_user_id, candidate.outbox_id,
      candidate.claim_id, candidate.revision_id, candidate.revision_number,
      candidate.operation_id,
      candidate.operation,
      candidate.sequence_number, candidate.point_id,
      candidate.collection_alias,
      candidate.revision_sha256, candidate.selection_binding_sha256,
      candidate.predicate_catalog_sha256,
      memory_private.projection_contract_sha256(), 3072,
      'text-embedding-3-large'::text,
      'f77b782b3e549b30a46b4beb7e25b248018f6c0f3597b36d0020103570e66442',
      candidate.projection_manifest_sha256,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.retrieval_text ELSE NULL::text END,
      candidate.retrieval_text_sha256,
      candidate.retrieval_text_sha256,
      candidate.source_sha256, candidate.lifecycle_state,
      candidate.current_revision_id = candidate.revision_id,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.predicate ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.epistemic_state ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.sensitivity ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.domains ELSE ARRAY[]::text[] END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.intents ELSE ARRAY[]::text[] END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.surface ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.requires_explicit ELSE false END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.projectable ELSE false END,
      candidate.valid_from, candidate.valid_to, candidate.claim_updated_at,
      candidate.current_state_sha256, candidate.semantic_key_sha256,
      candidate.claim_identity_sha256,
      candidate.subject_entity_key, candidate.subject_entity_type,
      candidate.subject_display_name,
      candidate.object_kind, candidate.object_entity_key,
      candidate.object_entity_type, candidate.object_display_name,
      candidate.object_literal,
      new_lease_token, new_lease_expires_at;
  END LOOP;
END;
$function$;

CREATE OR REPLACE FUNCTION memory_private.seal_source_erasure(p_operation_id uuid)
RETURNS TABLE(outcome text, state text, touched_claim_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  operation memory.source_erasure_operation%ROWTYPE;
  recomputed_manifest text;
  observed_target_count integer;
  touched_count integer;
  dispatched_count integer;
  locked_message_id uuid;
  sealed_timestamp timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' OR p_operation_id IS NULL THEN
    RAISE EXCEPTION 'invalid source erasure seal request'
      USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT operation
  FROM memory.source_erasure_operation AS value
  WHERE value.operation_id = p_operation_id
  FOR UPDATE;
  IF operation.state <> 'receiving' THEN
    RETURN QUERY SELECT 'replayed'::text, operation.state,
      operation.touched_claim_count;
    RETURN;
  END IF;
  LOCK TABLE memory.answer_binding IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.claim_evidence IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.projection_outbox IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.proposal IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.claim_revision IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.claim IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.provider_call IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.extraction_job IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.evidence IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.entity IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.source_erasure_claim IN ROW EXCLUSIVE MODE;
  LOCK TABLE memory.source_erasure_target IN ROW EXCLUSIVE MODE;
  PERFORM memory_private.assert_source_erasure_deletion_catalog();
  SELECT pg_catalog.count(*)::integer,
    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.source_erasure_target_manifest.v1' || E'\n'
        || COALESCE(pg_catalog.string_agg(
          target.target_sha256, E'\n'
          ORDER BY target.source_created_at, target.message_id
        ), ''),
      'UTF8'
    )), 'hex')
  INTO observed_target_count, recomputed_manifest
  FROM memory.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id;
  IF observed_target_count <> operation.target_count
     OR operation.received_target_count <> operation.target_count
     OR recomputed_manifest <> operation.target_manifest_sha256 THEN
    RAISE EXCEPTION 'source erasure target manifest is incomplete'
      USING ERRCODE = '40001';
  END IF;

  sealed_timestamp := pg_catalog.transaction_timestamp();
  FOR locked_message_id IN
    SELECT target.message_id
    FROM memory.source_erasure_target AS target
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
    ORDER BY target.message_id
  LOOP
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        'governed_memory.erased_chat_message.v1|'
          || locked_message_id::text,
        0
      )
    );
  END LOOP;
  IF EXISTS (
    SELECT 1
    FROM memory.evidence AS evidence
    JOIN memory.source_erasure_target AS target
      ON target.operation_id = operation.operation_id
     AND target.message_id IN (
       evidence.source_message_id, evidence.context_message_id
     )
    WHERE target.owner_user_id = operation.owner_user_id
      AND evidence.source_kind = 'conversation_message'
      AND evidence.owner_user_id <> operation.owner_user_id
  ) OR EXISTS (
    SELECT 1
    FROM memory.answer_binding AS binding
    JOIN memory.source_erasure_target AS target
      ON target.operation_id = operation.operation_id
     AND target.message_id = binding.response_id
    WHERE target.owner_user_id = operation.owner_user_id
      AND binding.owner_user_id <> operation.owner_user_id
  ) THEN
    UPDATE memory.source_erasure_operation AS value
    SET state = 'manual_review', sealed_at = sealed_timestamp,
        last_error_code = 'cross_owner_chat_message_lineage'
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id;
    RETURN QUERY SELECT 'manual_review'::text, 'manual_review'::text, 0;
    RETURN;
  END IF;
  INSERT INTO memory.erased_chat_message_tombstone(
    message_id, owner_user_id, erasure_operation_id, erased_at
  )
  SELECT target.message_id, operation.owner_user_id,
    operation.operation_id, sealed_timestamp
  FROM memory.source_erasure_target AS target
  WHERE target.owner_user_id = operation.owner_user_id
    AND target.operation_id = operation.operation_id
  ORDER BY target.message_id
  ON CONFLICT (message_id) DO NOTHING;
  IF (
    SELECT pg_catalog.count(*)
    FROM memory.source_erasure_target AS target
    JOIN memory.erased_chat_message_tombstone AS tombstone
      ON tombstone.message_id = target.message_id
     AND tombstone.owner_user_id = operation.owner_user_id
     AND tombstone.erasure_operation_id = operation.operation_id
     AND tombstone.erased_at = sealed_timestamp
    WHERE target.owner_user_id = operation.owner_user_id
      AND target.operation_id = operation.operation_id
  ) <> observed_target_count THEN
    RAISE EXCEPTION 'erased chat message tombstone lineage conflicts'
      USING ERRCODE = '23514';
  END IF;

  WITH targeted_evidence AS (
    SELECT DISTINCT evidence.evidence_id
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id = operation.owner_user_id
      AND evidence.source_kind = 'conversation_message'
      AND (
        EXISTS (
          SELECT 1 FROM memory.source_erasure_target AS target
          WHERE target.owner_user_id = evidence.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.message_id = evidence.source_message_id
        )
        OR EXISTS (
          SELECT 1 FROM memory.source_erasure_target AS target
          WHERE target.owner_user_id = evidence.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.message_id = evidence.context_message_id
        )
      )
  ), touched AS (
    SELECT link.claim_id
    FROM memory.claim_evidence AS link
    JOIN targeted_evidence AS target
      ON target.evidence_id = link.evidence_id
    WHERE link.owner_user_id = operation.owner_user_id
    UNION
    SELECT revision.claim_id
    FROM memory.claim_revision AS revision
    JOIN memory.proposal AS proposal
      ON proposal.owner_user_id = revision.owner_user_id
     AND proposal.proposal_id = revision.source_proposal_id
    JOIN targeted_evidence AS target
      ON target.evidence_id = proposal.evidence_id
    WHERE revision.owner_user_id = operation.owner_user_id
    UNION
    SELECT proposal.correction_of_claim_id
    FROM memory.proposal AS proposal
    JOIN targeted_evidence AS target
      ON target.evidence_id = proposal.evidence_id
    WHERE proposal.owner_user_id = operation.owner_user_id
      AND proposal.correction_of_claim_id IS NOT NULL
  )
  INSERT INTO memory.source_erasure_claim(
    owner_user_id, operation_id, claim_id, claim_delete_operation_id,
    prior_state_sha256, revision_id, revision_sha256
  )
  SELECT claim.owner_user_id, operation.operation_id, claim.claim_id,
    memory_private.uuid5(
      operation.operation_id, 'claim-delete:' || claim.claim_id::text
    ),
    claim.current_state_sha256, revision.revision_id,
    revision.revision_sha256
  FROM touched
  JOIN memory.claim AS claim
    ON claim.owner_user_id = operation.owner_user_id
   AND claim.claim_id = touched.claim_id
  JOIN memory.claim_revision AS revision
    ON revision.owner_user_id = claim.owner_user_id
   AND revision.claim_id = claim.claim_id
   AND revision.revision_id = claim.current_revision_id
  WHERE claim.lifecycle_state IN ('active', 'correction_pending', 'retracted')
  ORDER BY claim.claim_id;

  IF EXISTS (
    WITH targeted_evidence AS (
      SELECT evidence.evidence_id
      FROM memory.evidence AS evidence
      WHERE evidence.owner_user_id = operation.owner_user_id
        AND EXISTS (
          SELECT 1 FROM memory.source_erasure_target AS target
          WHERE target.owner_user_id = evidence.owner_user_id
            AND target.operation_id = operation.operation_id
            AND target.message_id IN (
              evidence.source_message_id, evidence.context_message_id
            )
        )
    )
    SELECT 1
    FROM memory.claim_evidence AS link
    JOIN targeted_evidence AS target ON target.evidence_id = link.evidence_id
    JOIN memory.claim AS claim
      ON claim.owner_user_id = link.owner_user_id
     AND claim.claim_id = link.claim_id
    WHERE link.owner_user_id = operation.owner_user_id
      AND claim.lifecycle_state = 'deletion_pending'
  ) THEN
    UPDATE memory.source_erasure_operation AS value
    SET state = 'manual_review', sealed_at = sealed_timestamp,
        last_error_code = 'overlapping_claim_deletion'
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id;
    RETURN QUERY SELECT 'manual_review'::text, 'manual_review'::text, 0;
    RETURN;
  END IF;

  SELECT pg_catalog.count(*)::integer INTO touched_count
  FROM memory.source_erasure_claim AS source_claim
  WHERE source_claim.owner_user_id = operation.owner_user_id
    AND source_claim.operation_id = operation.operation_id;
  IF EXISTS (
    SELECT 1
    FROM memory.source_erasure_claim AS source_claim
    JOIN memory.projection_outbox AS outbox
      ON outbox.owner_user_id = source_claim.owner_user_id
     AND outbox.claim_id = source_claim.claim_id
    WHERE source_claim.owner_user_id = operation.owner_user_id
      AND source_claim.operation_id = operation.operation_id
      AND outbox.operation = 'upsert'
      AND outbox.embedding_request_sha256 IS NOT NULL
      AND outbox.state = 'claimed'
  ) THEN
    UPDATE memory.source_erasure_operation AS value
    SET state = 'manual_review', sealed_at = sealed_timestamp,
        touched_claim_count = touched_count,
        last_error_code = 'pre_fence_projection_dispatch_uncertain'
    WHERE value.owner_user_id = operation.owner_user_id
      AND value.operation_id = operation.operation_id;
    RETURN QUERY SELECT 'manual_review'::text, 'manual_review'::text,
      touched_count;
    RETURN;
  END IF;
  SELECT pg_catalog.count(*)::integer INTO dispatched_count
  FROM memory.provider_call AS call
  JOIN memory.extraction_job AS job
    ON job.owner_user_id = call.owner_user_id
   AND job.job_id = call.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = job.owner_user_id
   AND evidence.evidence_id = job.evidence_id
  WHERE call.owner_user_id = operation.owner_user_id
    AND call.state IN ('dispatched', 'completed', 'outcome_unknown')
    AND EXISTS (
      SELECT 1 FROM memory.source_erasure_target AS target
      WHERE target.owner_user_id = evidence.owner_user_id
        AND target.operation_id = operation.operation_id
        AND target.message_id IN (
          evidence.source_message_id, evidence.context_message_id
        )
    );
  dispatched_count := dispatched_count + (
    SELECT pg_catalog.count(*)::integer
    FROM memory.source_erasure_claim AS source_claim
    JOIN memory.projection_outbox AS outbox
      ON outbox.owner_user_id = source_claim.owner_user_id
     AND outbox.claim_id = source_claim.claim_id
    WHERE source_claim.owner_user_id = operation.owner_user_id
      AND source_claim.operation_id = operation.operation_id
      AND outbox.operation = 'upsert'
      AND outbox.state = 'failed_terminal'
      AND outbox.embedding_request_sha256 IS NOT NULL
      AND outbox.embedding_dispatched_at IS NOT NULL
  );
  UPDATE memory.source_erasure_operation AS value
  SET state = CASE WHEN touched_count = 0
        THEN 'fenced' ELSE 'claim_deletion_pending' END,
      received_target_count = observed_target_count,
      touched_claim_count = touched_count,
      pre_fence_provider_dispatch_count = dispatched_count,
      sealed_at = sealed_timestamp,
      last_error_code = NULL
  WHERE value.owner_user_id = operation.owner_user_id
    AND value.operation_id = operation.operation_id;
  RETURN QUERY SELECT 'sealed'::text,
    CASE WHEN touched_count = 0
      THEN 'fenced'::text ELSE 'claim_deletion_pending'::text END,
    touched_count;
END;
$function$;
