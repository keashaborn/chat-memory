SET ROLE governed_memory_owner;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE memory.proposal
  DROP CONSTRAINT proposal_review_shape,
  DROP CONSTRAINT proposal_review_reason_codes;
ALTER TABLE memory.proposal
  ADD CONSTRAINT proposal_review_shape CHECK (
    (review_state = 'pending_review' AND reviewer_kind IS NULL
      AND reviewer_user_id IS NULL AND review_reason_codes IS NULL
      AND reviewed_at IS NULL)
    OR (review_state = 'admitted' AND reviewer_kind = 'owner'
      AND reviewer_user_id IS NOT NULL
      AND review_reason_codes = ARRAY['explicit_owner_review']::text[]
      AND reviewed_at IS NOT NULL)
    OR (review_state = 'admitted' AND reviewer_kind = 'system'
      AND reviewer_user_id IS NULL
      AND review_reason_codes = ARRAY['automatic_low_risk_owner_assertion']::text[]
      AND reviewed_at IS NOT NULL)
    OR (review_state = 'rejected' AND reviewer_kind = 'owner'
      AND reviewer_user_id IS NOT NULL
      AND memory_private.is_sorted_unique_allowlist(review_reason_codes,
        ARRAY['duplicate_existing','not_durable','proposal_incorrect']::text[])
      AND reviewed_at IS NOT NULL)
    OR (review_state = 'rejected' AND reviewer_kind = 'system'
      AND reviewer_user_id IS NULL
      AND review_reason_codes = ARRAY['lifecycle_override']::text[]
      AND reviewed_at IS NOT NULL)
    OR (review_state = 'expired' AND reviewer_kind = 'system'
      AND reviewer_user_id IS NULL
      AND review_reason_codes = ARRAY['proposal_expired']::text[]
      AND reviewed_at IS NOT NULL)
  ),
  ADD CONSTRAINT proposal_review_reason_codes CHECK (
    review_reason_codes IS NULL OR (
      pg_catalog.cardinality(review_reason_codes) BETWEEN 1 AND 3
      AND memory_private.is_ascii_key_array(review_reason_codes)
      AND memory_private.is_sorted_unique_allowlist(review_reason_codes,
        ARRAY['automatic_low_risk_owner_assertion','duplicate_existing',
          'explicit_owner_review','lifecycle_override','not_durable',
          'proposal_expired','proposal_incorrect']::text[])
    )
  );
ALTER TABLE memory.claim_evidence
  DROP CONSTRAINT claim_evidence_reason_codes;
ALTER TABLE memory.claim_evidence
  ADD CONSTRAINT claim_evidence_reason_codes CHECK (
    pg_catalog.cardinality(reason_codes) BETWEEN 1 AND 3
    AND memory_private.is_ascii_key_array(reason_codes)
    AND memory_private.is_sorted_unique_allowlist(reason_codes,
      ARRAY['automatic_low_risk_owner_assertion','duplicate_existing',
        'explicit_owner_review','not_durable','proposal_incorrect']::text[])
  );
CREATE OR REPLACE FUNCTION memory_private.review_proposal(
  p_operation_id uuid,
  p_proposal_id uuid,
  p_decision text,
  p_expected_proposal_sha256 text,
  p_expected_source_sha256 text,
  p_expected_selected_sha256 text,
  p_expected_selection_binding_sha256 text,
  p_expected_predicate_catalog_sha256 text,
  p_reason_codes text[]
)
RETURNS TABLE(
  outcome text,
  claim_id uuid,
  revision_id uuid,
  outbox_id uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  preview_correction_claim_id uuid;
  proposal memory.proposal%ROWTYPE;
  proposal_evidence memory.evidence%ROWTYPE;
  proposal_call memory.provider_call%ROWTYPE;
  target_claim memory.claim%ROWTYPE;
  current_revision memory.claim_revision%ROWTYPE;
  subject_id uuid;
  object_id uuid;
  resulting_claim_id uuid;
  resulting_revision_id uuid;
  resulting_outbox_id uuid;
  resulting_revision_number integer;
  resulting_sequence integer;
  retrieval_text text;
  retrieval_hash text;
  fact_policy_hash text;
  revision_hash text;
  claim_identity_hash text;
  prior_state_hash text;
  new_state_hash text;
  transition_at timestamptz;
  projection_hash text;
  projection_operation_id uuid;
  existing_projection memory.projection_outbox%ROWTYPE;
  event_hash text;
  restore_event_hash text;
  recomputed_proposal_hash text;
  automatic_admission boolean;
BEGIN
  automatic_admission := session_user = 'governed_memory_worker';
  IF session_user NOT IN ('governed_memory_api', 'governed_memory_worker') THEN
    RAISE EXCEPTION 'proposal admission role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_proposal_id IS NULL
     OR p_decision IS NULL OR p_decision NOT IN ('admitted', 'rejected')
     OR p_expected_proposal_sha256 IS NULL
     OR p_expected_proposal_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_source_sha256 IS NULL
     OR p_expected_source_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selected_sha256 IS NULL
     OR p_expected_selected_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selection_binding_sha256 IS NULL
     OR p_expected_selection_binding_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_predicate_catalog_sha256 IS NULL
     OR p_expected_predicate_catalog_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_codes IS NULL
     OR (
       p_decision = 'admitted'
       AND (
         (NOT automatic_admission AND p_reason_codes IS DISTINCT FROM
           ARRAY['explicit_owner_review']::text[])
         OR (automatic_admission AND p_reason_codes IS DISTINCT FROM
           ARRAY['automatic_low_risk_owner_assertion']::text[])
       )
     )
     OR (automatic_admission AND p_decision <> 'admitted')
     OR (
       p_decision = 'rejected'
       AND NOT memory_private.is_sorted_unique_allowlist(
         p_reason_codes,
         ARRAY[
           'duplicate_existing', 'not_durable', 'proposal_incorrect'
         ]::text[]
       )
     ) THEN
    RAISE EXCEPTION 'invalid proposal review input' USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text, 0
    )
  );
  SELECT value.correction_of_claim_id INTO preview_correction_claim_id
  FROM memory.proposal AS value
  WHERE value.owner_user_id = actor AND value.proposal_id = p_proposal_id;
  IF NOT FOUND THEN
    IF EXISTS (
      SELECT 1
      FROM memory.audit_event AS receipt
      WHERE receipt.owner_user_id = actor
        AND receipt.object_type = 'proposal'
        AND receipt.object_id = p_proposal_id
        AND receipt.transition_code = 'proposal_retention_purged'
        AND receipt.reason_code = 'retention_expired'
    ) THEN
      RAISE EXCEPTION 'proposal_retention_purged'
        USING ERRCODE = 'P0002';
    END IF;
    RAISE EXCEPTION 'proposal not found' USING ERRCODE = 'P0002';
  END IF;
  IF preview_correction_claim_id IS NOT NULL THEN
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        actor::text || '|claim|'
          || preview_correction_claim_id::text,
        0
      )
    );
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|proposal|' || p_proposal_id::text, 0
    )
  );
  SELECT value.* INTO STRICT proposal
  FROM memory.proposal AS value
  WHERE value.owner_user_id = actor AND value.proposal_id = p_proposal_id
  FOR UPDATE;
  IF proposal.operation_id IS DISTINCT FROM p_operation_id
     OR proposal.correction_of_claim_id
          IS DISTINCT FROM preview_correction_claim_id THEN
    RAISE EXCEPTION 'proposal lock preview changed'
      USING ERRCODE = '40001';
  END IF;
  SELECT value.* INTO STRICT proposal_evidence
  FROM memory.evidence AS value
  WHERE value.owner_user_id = actor
    AND value.evidence_id = proposal.evidence_id;
  IF proposal.provider_call_id IS NOT NULL THEN
    SELECT value.* INTO STRICT proposal_call
    FROM memory.provider_call AS value
    WHERE value.owner_user_id = actor
      AND value.provider_call_id = proposal.provider_call_id;
  END IF;
  IF automatic_admission AND (
       pg_catalog.current_setting('app.auth_context_sha256', true)
         <> '421ec318b932459828e25f2ed52fc3cc589bd09ee333fb4d77dbbddf61450432'
       OR proposal.proposal_purpose <> 'new_claim'
       OR proposal.correction_of_claim_id IS NOT NULL
       OR proposal_evidence.source_kind <> 'conversation_message'
       OR proposal_evidence.eligibility_decision <> 'send_external'
       OR proposal_call.state <> 'completed'
       OR proposal.subject_entity_type <> 'self'
       OR proposal.subject_entity_key <> 'self'
       OR proposal.epistemic_state <> 'supported'
       OR proposal.sensitivity <> 'ordinary'
       OR NOT proposal.projectable
       OR proposal.surface <> 'normal'
       OR proposal.requires_explicit
       OR pg_catalog.cardinality(proposal.domains) <> 0
       OR pg_catalog.cardinality(proposal.intents) <> 0
       OR proposal.valid_from IS NOT NULL
       OR proposal.valid_to IS NOT NULL
       OR proposal.expires_at <= pg_catalog.transaction_timestamp()
       OR memory_private.owner_source_erasure_active(actor)
       OR NOT EXISTS (
         SELECT 1 FROM memory.extraction_job AS bounded_job
         WHERE bounded_job.owner_user_id = actor
           AND bounded_job.job_id = proposal_call.job_id
           AND bounded_job.evidence_id = proposal.evidence_id
           AND bounded_job.state = 'completed'
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.pilot_marker AS bounded_marker
         WHERE bounded_marker.pilot_ever_started
           AND proposal_evidence.source_created_at >= bounded_marker.started_at
       )
       OR EXISTS (
         SELECT 1 FROM memory.claim AS bounded_claim
         WHERE bounded_claim.owner_user_id = actor
           AND bounded_claim.semantic_key_sha256 = proposal.semantic_key_sha256
       )
     ) THEN
    RAISE EXCEPTION 'automatic admission policy denied'
      USING ERRCODE = '42501';
  END IF;
  recomputed_proposal_hash := memory_private.proposal_sha256(
    actor,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.job_id ELSE NULL::uuid END,
    proposal.evidence_id, proposal.provider_call_id,
    proposal_evidence.source_kind, proposal_evidence.source_message_id,
    proposal_evidence.source_thread_id, proposal_evidence.source_window_id,
    proposal_evidence.source_window_sha256, proposal_evidence.source_sha256,
    proposal.selected_sha256, proposal.selection_binding_sha256,
    proposal_evidence.context_message_id, proposal_evidence.context_sha256,
    proposal.predicate_catalog_sha256,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.request_sha256 ELSE NULL::text END,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.response_sha256 ELSE NULL::text END,
    proposal.proposal_purpose, proposal.correction_of_claim_id,
    proposal.correction_target_revision_id,
    proposal.correction_target_revision_number,
    proposal.expected_revision_sha256,
    proposal.correction_target_state_sha256,
    proposal.correction_target_identity_sha256,
    proposal.correction_target_projection_sequence,
    proposal.correction_pending_state_sha256, proposal.fact_index,
    proposal.proposal_id, proposal.operation_id,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate, proposal.object_kind,
    proposal.object_entity_type, proposal.object_entity_key,
    proposal.object_display_name, proposal.object_literal,
    proposal.epistemic_state, proposal.sensitivity,
    proposal.semantic_key_sha256,
    proposal.projectable, proposal.domains, proposal.intents,
    proposal.surface, proposal.requires_explicit, proposal.valid_from,
    proposal.valid_to
  );
  IF proposal.proposal_sha256 <> p_expected_proposal_sha256
     OR proposal.proposal_sha256 <> recomputed_proposal_hash
     OR proposal_evidence.source_sha256 <> p_expected_source_sha256
     OR proposal.selected_sha256 <> p_expected_selected_sha256
     OR proposal.selection_binding_sha256
          <> p_expected_selection_binding_sha256
     OR proposal.predicate_catalog_sha256
          <> p_expected_predicate_catalog_sha256
     OR proposal_evidence.selected_sha256 <> p_expected_selected_sha256
     OR proposal_evidence.selection_binding_sha256
          <> p_expected_selection_binding_sha256
     OR (
       proposal.provider_call_id IS NOT NULL
       AND (
         proposal_call.state <> 'completed'
         OR proposal_call.selected_sha256 <> proposal.selected_sha256
         OR proposal_call.selection_binding_sha256
              <> proposal.selection_binding_sha256
         OR proposal_call.predicate_catalog_sha256
              <> proposal.predicate_catalog_sha256
         OR NOT EXISTS (
           SELECT 1 FROM memory.extraction_job AS job
           WHERE job.owner_user_id = actor
             AND job.job_id = proposal_call.job_id
             AND job.evidence_id = proposal.evidence_id
             AND job.state = 'completed'
         )
       )
     )
     OR proposal.semantic_key_sha256 <> memory_private.semantic_key_sha256(
          proposal.subject_entity_key, proposal.predicate,
          proposal.object_kind, proposal.object_entity_key,
          proposal.object_literal
        )
     OR (
       proposal.proposal_purpose = 'correction'
       AND proposal.correction_target_identity_sha256
             <> memory_private.claim_identity_sha256(
                  proposal.subject_entity_key, proposal.predicate
                )
     )
     OR NOT proposal.projectable
     OR pg_catalog.cardinality(proposal.domains) <> 0
     OR pg_catalog.cardinality(proposal.intents) <> 0
     OR proposal.valid_from IS NOT NULL
     OR proposal.valid_to IS NOT NULL
     OR proposal.surface <> (
       CASE WHEN proposal.sensitivity = 'ordinary'
         THEN 'normal' ELSE 'explicit_only' END
     )
     OR proposal.requires_explicit
          <> (proposal.sensitivity <> 'ordinary')
     OR NOT EXISTS (
       SELECT 1 FROM memory.predicate_catalog AS predicate
       WHERE predicate.active
         AND predicate.predicate = proposal.predicate
         AND predicate.catalog_sha256 = p_expected_predicate_catalog_sha256
         AND proposal.subject_entity_type = ANY(predicate.subject_kinds)
         AND proposal.object_kind = ANY(predicate.object_kinds)
         AND proposal.sensitivity = ANY(predicate.sensitivities)
         AND proposal.epistemic_state = ANY(predicate.epistemic_statuses)
     ) THEN
    RAISE EXCEPTION 'proposal evidence or catalog CAS mismatch'
      USING ERRCODE = '40001';
  END IF;
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|'
      || proposal.proposal_id::text || '|' || p_decision || '|'
      || proposal.proposal_sha256 || '|' || proposal_evidence.source_sha256
      || '|' || proposal.selected_sha256 || '|'
      || proposal.selection_binding_sha256 || '|'
      || proposal.predicate_catalog_sha256 || '|'
      || memory_private.framed_text_array(
           'reason_codes', p_reason_codes
         ), 'UTF8'
    )
  ), 'hex');

  IF proposal.review_state <> 'pending_review' THEN
    IF proposal.review_state <> p_decision
       OR proposal.reviewer_kind IS DISTINCT FROM (
            CASE WHEN automatic_admission THEN 'system' ELSE 'owner' END
          )
       OR proposal.reviewer_user_id IS DISTINCT FROM (
            CASE WHEN automatic_admission THEN NULL::uuid ELSE actor END
          )
       OR proposal.review_reason_codes IS DISTINCT FROM p_reason_codes
       OR NOT EXISTS (
         SELECT 1 FROM memory.audit_event AS receipt
         WHERE receipt.owner_user_id = actor
           AND receipt.operation_id = p_operation_id
           AND receipt.transition_code = CASE
             WHEN p_decision = 'admitted' THEN 'proposal_admitted'
             ELSE 'proposal_rejected'
           END
           AND receipt.reason_code = CASE
             WHEN p_decision = 'admitted' THEN 'proposal_admitted'
             ELSE 'proposal_rejected'
           END
           AND receipt.event_sha256 = event_hash
       ) THEN
      RAISE EXCEPTION 'proposal already resolved differently'
        USING ERRCODE = '23514';
    END IF;
    IF p_decision = 'admitted' THEN
      SELECT revision.claim_id, revision.revision_id
      INTO STRICT resulting_claim_id, resulting_revision_id
      FROM memory.claim_revision AS revision
      WHERE revision.owner_user_id = actor
        AND revision.source_proposal_id = proposal.proposal_id;
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'admission-projection'
      );
      SELECT value.* INTO STRICT existing_projection
      FROM memory.projection_outbox AS value
      WHERE value.owner_user_id = actor
        AND value.operation_id = projection_operation_id;
      IF existing_projection.claim_id <> resulting_claim_id
         OR existing_projection.revision_id <> resulting_revision_id
         OR existing_projection.operation <> 'upsert'
         OR existing_projection.point_id <> resulting_claim_id THEN
        RAISE EXCEPTION 'proposal admission replay lineage drifted'
          USING ERRCODE = '23514';
      END IF;
      resulting_outbox_id := existing_projection.outbox_id;
    ELSIF proposal.correction_of_claim_id IS NOT NULL THEN
      resulting_claim_id := proposal.correction_of_claim_id;
      resulting_revision_id := NULL::uuid;
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'correction-rejection-projection'
      );
      SELECT value.* INTO STRICT existing_projection
      FROM memory.projection_outbox AS value
      WHERE value.owner_user_id = actor
        AND value.operation_id = projection_operation_id;
      IF existing_projection.claim_id <> proposal.correction_of_claim_id
         OR existing_projection.operation <> 'upsert'
         OR existing_projection.point_id <> proposal.correction_of_claim_id
         OR NOT EXISTS (
           SELECT 1 FROM memory.audit_event AS restored
           WHERE restored.owner_user_id = actor
             AND restored.operation_id = p_operation_id
             AND restored.transition_code = 'correction_rejected_restored'
             AND restored.object_type = 'claim'
             AND restored.object_id = proposal.correction_of_claim_id
             AND restored.reason_code = 'proposal_rejected'
         ) THEN
        RAISE EXCEPTION 'proposal rejection replay lineage drifted'
          USING ERRCODE = '23514';
      END IF;
      resulting_outbox_id := existing_projection.outbox_id;
    ELSE
      resulting_claim_id := NULL::uuid;
      resulting_revision_id := NULL::uuid;
      resulting_outbox_id := NULL::uuid;
    END IF;
    RETURN QUERY SELECT 'replayed'::text, resulting_claim_id,
      resulting_revision_id, resulting_outbox_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, p_proposal_id, NULL::uuid,
       proposal.correction_of_claim_id, ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;
  IF proposal.expires_at <= pg_catalog.transaction_timestamp() THEN
    RAISE EXCEPTION 'proposal expired' USING ERRCODE = '40001';
  END IF;

  IF p_decision = 'rejected' THEN
    UPDATE memory.proposal AS stored_proposal
    SET review_state = 'rejected', reviewer_kind = 'owner',
        reviewer_user_id = actor,
        review_reason_codes = p_reason_codes,
        reviewed_at = pg_catalog.transaction_timestamp()
    WHERE stored_proposal.owner_user_id = actor
      AND stored_proposal.proposal_id = proposal.proposal_id;
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, actor_user_id,
      object_type, object_id, transition_code, prior_state_sha256,
      new_state_sha256, reason_code, event_sha256
    ) VALUES (
      actor, p_operation_id, 'reviewer', actor, 'proposal',
      proposal.proposal_id, 'proposal_rejected', proposal.proposal_sha256,
      event_hash, 'proposal_rejected', event_hash
    );
    IF proposal.correction_of_claim_id IS NOT NULL THEN
      SELECT value.* INTO STRICT target_claim
      FROM memory.claim AS value
      WHERE value.owner_user_id = actor
        AND value.claim_id = proposal.correction_of_claim_id
      FOR UPDATE;
      SELECT value.* INTO STRICT current_revision
      FROM memory.claim_revision AS value
      WHERE value.owner_user_id = actor
        AND value.claim_id = target_claim.claim_id
        AND value.revision_id = target_claim.current_revision_id;
      IF target_claim.lifecycle_state <> 'correction_pending'
         OR target_claim.claim_identity_sha256
              <> proposal.correction_target_identity_sha256
         OR target_claim.projection_sequence
              <> proposal.correction_target_projection_sequence + 1
         OR target_claim.current_state_sha256
              <> proposal.correction_pending_state_sha256
         OR current_revision.revision_id
              <> proposal.correction_target_revision_id
         OR current_revision.revision_number
              <> proposal.correction_target_revision_number
         OR current_revision.revision_sha256
              <> proposal.expected_revision_sha256 THEN
        RAISE EXCEPTION 'correction rejection target state changed'
          USING ERRCODE = '40001';
      END IF;
      prior_state_hash := memory_private.claim_state_sha256(
        target_claim.owner_user_id, target_claim.claim_id,
        target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
        target_claim.lifecycle_state, true,
        target_claim.current_revision_id, target_claim.current_revision_number,
        current_revision.revision_sha256, target_claim.projection_sequence
      );
      IF prior_state_hash <> target_claim.current_state_sha256
         OR NOT EXISTS (
           SELECT 1 FROM memory.audit_event AS transition
           WHERE transition.owner_user_id = actor
             AND transition.operation_id = proposal_evidence.operation_id
             AND transition.object_type = 'claim'
             AND transition.object_id = target_claim.claim_id
             AND transition.transition_code = 'correction_requested'
             AND transition.prior_state_sha256
                   = proposal.correction_target_state_sha256
             AND transition.new_state_sha256 = target_claim.current_state_sha256
         ) THEN
        RAISE EXCEPTION 'correction rejection state hash changed'
          USING ERRCODE = '40001';
      END IF;
      resulting_sequence := target_claim.projection_sequence + 1;
      resulting_outbox_id := pg_catalog.gen_random_uuid();
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'correction-rejection-projection'
      );
      transition_at := pg_catalog.transaction_timestamp();
      new_state_hash := memory_private.claim_state_sha256(
        target_claim.owner_user_id, target_claim.claim_id,
        target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
        'active', true,
        target_claim.current_revision_id, target_claim.current_revision_number,
        current_revision.revision_sha256, resulting_sequence
      );
      projection_hash := memory_private.projection_manifest_sha256(
        actor, target_claim.claim_id, current_revision.revision_id,
        projection_operation_id, 'upsert', resulting_sequence,
        current_revision.revision_sha256,
        current_revision.selection_binding_sha256,
        current_revision.retrieval_text_sha256,
        current_revision.retrieval_text_sha256
      );
      UPDATE memory.claim AS target_row
      SET lifecycle_state = 'active', current_state_sha256 = new_state_hash,
          correction_pending_at = NULL,
          projection_sequence = resulting_sequence,
          updated_at = transition_at
      WHERE target_row.owner_user_id = actor
        AND target_row.claim_id = target_claim.claim_id;
      INSERT INTO memory.projection_outbox(
        outbox_id, owner_user_id, claim_id, revision_id, operation_id,
        sequence_number, operation, point_id, revision_sha256,
        selection_binding_sha256, projection_manifest_sha256
      ) VALUES (
        resulting_outbox_id, actor, target_claim.claim_id,
        current_revision.revision_id, projection_operation_id,
        resulting_sequence, 'upsert', target_claim.claim_id,
        current_revision.revision_sha256,
        current_revision.selection_binding_sha256, projection_hash
      );
      restore_event_hash := pg_catalog.encode(pg_catalog.sha256(
        pg_catalog.convert_to(
          actor::text || '|' || p_operation_id::text
          || '|correction_rejected_restored|'
          || target_claim.claim_id::text || '|'
          || current_revision.revision_sha256, 'UTF8'
        )
      ), 'hex');
      INSERT INTO memory.audit_event(
        owner_user_id, operation_id, actor_kind, actor_user_id,
        object_type, object_id, transition_code, prior_state_sha256,
        new_state_sha256, reason_code, event_sha256
      ) VALUES (
        actor, p_operation_id, 'reviewer', actor, 'claim',
        target_claim.claim_id, 'correction_rejected_restored',
        prior_state_hash, new_state_hash,
        'proposal_rejected', restore_event_hash
      );
    END IF;
    RETURN QUERY SELECT 'rejected'::text,
      proposal.correction_of_claim_id, NULL::uuid, resulting_outbox_id;
    RETURN;
  END IF;

  INSERT INTO memory.entity(
    owner_user_id, entity_key, entity_type, display_name, normalized_name
  ) VALUES (
    actor, proposal.subject_entity_key, proposal.subject_entity_type,
    COALESCE(proposal.subject_display_name, proposal.subject_entity_key),
    pg_catalog.lower(
      COALESCE(proposal.subject_display_name, proposal.subject_entity_key)
    )
  ) ON CONFLICT (owner_user_id, entity_key) DO NOTHING;
  SELECT entity.entity_id INTO STRICT subject_id
  FROM memory.entity AS entity
  WHERE entity.owner_user_id = actor
    AND entity.entity_key = proposal.subject_entity_key
    AND entity.entity_type = proposal.subject_entity_type;

  IF proposal.object_kind = 'entity' THEN
    INSERT INTO memory.entity(
      owner_user_id, entity_key, entity_type, display_name, normalized_name
    ) VALUES (
      actor, proposal.object_entity_key, proposal.object_entity_type,
      proposal.object_display_name, pg_catalog.lower(proposal.object_display_name)
    ) ON CONFLICT (owner_user_id, entity_key) DO NOTHING;
    SELECT entity.entity_id INTO STRICT object_id
    FROM memory.entity AS entity
    WHERE entity.owner_user_id = actor
      AND entity.entity_key = proposal.object_entity_key
      AND entity.entity_type = proposal.object_entity_type;
  END IF;

  retrieval_text := memory_private.render_relational_fact(
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal
  );
  IF pg_catalog.octet_length(retrieval_text) NOT BETWEEN 1 AND 4096 THEN
    RAISE EXCEPTION 'deterministic retrieval rendering exceeds bound'
      USING ERRCODE = '22001';
  END IF;
  retrieval_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(retrieval_text, 'UTF8')
  ), 'hex');
  fact_policy_hash := memory_private.revision_fact_policy_sha256(
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, proposal.epistemic_state,
    proposal.sensitivity, proposal.projectable, proposal.domains,
    proposal.intents, proposal.surface, proposal.requires_explicit,
    proposal.valid_from, proposal.valid_to, proposal.selected_sha256,
    proposal.selection_binding_sha256, proposal.predicate_catalog_sha256,
    retrieval_hash
  );
  claim_identity_hash := memory_private.claim_identity_sha256(
    proposal.subject_entity_key, proposal.predicate
  );

  resulting_revision_id := pg_catalog.gen_random_uuid();
  transition_at := pg_catalog.transaction_timestamp();
  IF proposal.correction_of_claim_id IS NULL THEN
    IF EXISTS (
      SELECT 1 FROM memory.claim AS existing_claim
      WHERE existing_claim.owner_user_id = actor
        AND existing_claim.semantic_key_sha256 = proposal.semantic_key_sha256
    ) THEN
      RAISE EXCEPTION 'exact semantic identity already exists'
        USING ERRCODE = '23505';
    END IF;
    resulting_claim_id := pg_catalog.gen_random_uuid();
    resulting_revision_number := 1;
    resulting_sequence := 1;
  ELSE
    SELECT value.* INTO STRICT target_claim
    FROM memory.claim AS value
    WHERE value.owner_user_id = actor
      AND value.claim_id = proposal.correction_of_claim_id
    FOR UPDATE;
    IF target_claim.lifecycle_state <> 'correction_pending'
       OR target_claim.claim_identity_sha256
            <> proposal.correction_target_identity_sha256
       OR target_claim.projection_sequence
            <> proposal.correction_target_projection_sequence + 1
       OR target_claim.current_state_sha256
            <> proposal.correction_pending_state_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.claim_revision AS expected_revision
         WHERE expected_revision.owner_user_id = actor
           AND expected_revision.claim_id = target_claim.claim_id
           AND expected_revision.revision_id = target_claim.current_revision_id
           AND expected_revision.revision_id
                 = proposal.correction_target_revision_id
           AND expected_revision.revision_number
                 = proposal.correction_target_revision_number
           AND expected_revision.revision_sha256 = proposal.expected_revision_sha256
       ) THEN
      RAISE EXCEPTION 'correction target state changed' USING ERRCODE = '40001';
    END IF;
    prior_state_hash := memory_private.claim_state_sha256(
      target_claim.owner_user_id, target_claim.claim_id,
      target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
      target_claim.lifecycle_state, true,
      target_claim.current_revision_id, target_claim.current_revision_number,
      (SELECT expected_revision.revision_sha256
       FROM memory.claim_revision AS expected_revision
       WHERE expected_revision.owner_user_id = target_claim.owner_user_id
         AND expected_revision.revision_id = target_claim.current_revision_id),
      target_claim.projection_sequence
    );
    IF prior_state_hash <> target_claim.current_state_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.audit_event AS transition
         WHERE transition.owner_user_id = actor
           AND transition.operation_id = proposal_evidence.operation_id
           AND transition.object_type = 'claim'
           AND transition.object_id = target_claim.claim_id
           AND transition.transition_code = 'correction_requested'
           AND transition.prior_state_sha256
                 = proposal.correction_target_state_sha256
           AND transition.new_state_sha256 = target_claim.current_state_sha256
       ) THEN
      RAISE EXCEPTION 'correction target state hash changed'
        USING ERRCODE = '40001';
    END IF;
    resulting_claim_id := target_claim.claim_id;
    IF EXISTS (
      SELECT 1 FROM memory.claim AS existing_claim
      WHERE existing_claim.owner_user_id = actor
        AND existing_claim.semantic_key_sha256 = proposal.semantic_key_sha256
        AND existing_claim.claim_id <> target_claim.claim_id
    ) THEN
      RAISE EXCEPTION 'corrected semantic identity collides with another claim'
        USING ERRCODE = '23505';
    END IF;
    resulting_revision_number := target_claim.current_revision_number + 1;
    resulting_sequence := target_claim.projection_sequence + 1;
  END IF;

  revision_hash := memory_private.claim_revision_sha256(
    actor, resulting_claim_id, resulting_revision_id,
    resulting_revision_number, proposal.proposal_id,
    proposal.proposal_sha256, proposal.semantic_key_sha256,
    claim_identity_hash,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, proposal.epistemic_state,
    proposal.sensitivity, proposal.projectable, proposal.domains,
    proposal.intents, proposal.surface, proposal.requires_explicit,
    proposal.valid_from, proposal.valid_to, proposal_evidence.source_sha256,
    proposal.selected_sha256,
    proposal.selection_binding_sha256, proposal.predicate_catalog_sha256,
    retrieval_hash
  );

  new_state_hash := memory_private.claim_state_sha256(
    actor, resulting_claim_id, proposal.semantic_key_sha256,
    claim_identity_hash, 'active', true,
    resulting_revision_id, resulting_revision_number, revision_hash,
    resulting_sequence
  );

  IF proposal.correction_of_claim_id IS NULL THEN
    INSERT INTO memory.claim(
      claim_id, owner_user_id, semantic_key_sha256, claim_identity_sha256,
      current_state_sha256,
      lifecycle_state, current_revision_id, current_revision_number,
      projection_sequence, created_at, updated_at
    ) VALUES (
      resulting_claim_id, actor, proposal.semantic_key_sha256,
      claim_identity_hash, new_state_hash, 'active', resulting_revision_id,
      resulting_revision_number, resulting_sequence,
      transition_at, transition_at
    );
  END IF;

  INSERT INTO memory.claim_revision(
    revision_id, owner_user_id, claim_id, revision_number,
    source_proposal_id, semantic_key_sha256, claim_identity_sha256,
    subject_entity_id, subject_entity_type, subject_entity_key,
    subject_display_name, predicate, object_kind, object_entity_id,
    object_entity_type, object_entity_key, object_display_name,
    object_literal, retrieval_text, retrieval_text_sha256,
    epistemic_state, projectable,
    sensitivity, domains, intents, surface, requires_explicit,
    valid_from, valid_to, source_sha256, selected_sha256,
    selection_binding_sha256,
    predicate_catalog_sha256, fact_policy_sha256, revision_sha256
  ) VALUES (
    resulting_revision_id, actor, resulting_claim_id,
    resulting_revision_number, proposal.proposal_id,
    proposal.semantic_key_sha256, claim_identity_hash, subject_id,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, object_id, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, retrieval_text, retrieval_hash,
    proposal.epistemic_state, proposal.projectable, proposal.sensitivity,
    proposal.domains, proposal.intents, proposal.surface,
    proposal.requires_explicit, proposal.valid_from, proposal.valid_to,
    proposal_evidence.source_sha256,
    proposal.selected_sha256, proposal.selection_binding_sha256,
    proposal.predicate_catalog_sha256, fact_policy_hash, revision_hash
  );

  INSERT INTO memory.claim_evidence(
    owner_user_id, claim_id, revision_id, evidence_id,
    selected_sha256, selection_binding_sha256, stance, reason_codes
  ) SELECT actor, resulting_claim_id, resulting_revision_id,
      evidence.evidence_id, evidence.selected_sha256,
      evidence.selection_binding_sha256, 'supports', p_reason_codes
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id = actor
      AND evidence.evidence_id = proposal.evidence_id;

  IF proposal.correction_of_claim_id IS NOT NULL THEN
    UPDATE memory.claim AS target_row
    SET semantic_key_sha256 = proposal.semantic_key_sha256,
        claim_identity_sha256 = claim_identity_hash,
        current_state_sha256 = new_state_hash,
        lifecycle_state = 'active', current_revision_id = resulting_revision_id,
        current_revision_number = resulting_revision_number,
        projection_sequence = resulting_sequence,
        correction_pending_at = NULL, retracted_at = NULL,
        deletion_requested_at = NULL,
        updated_at = transition_at
    WHERE target_row.owner_user_id = actor
      AND target_row.claim_id = resulting_claim_id;
  END IF;

  UPDATE memory.proposal AS stored_proposal
  SET review_state = 'admitted',
      reviewer_kind = CASE WHEN automatic_admission
        THEN 'system' ELSE 'owner' END,
      reviewer_user_id = CASE WHEN automatic_admission
        THEN NULL::uuid ELSE actor END,
      review_reason_codes = p_reason_codes,
      reviewed_at = pg_catalog.transaction_timestamp()
  WHERE stored_proposal.owner_user_id = actor
    AND stored_proposal.proposal_id = proposal.proposal_id;

  IF proposal.projectable THEN
    resulting_outbox_id := pg_catalog.gen_random_uuid();
    projection_operation_id := memory_private.derived_uuid(
      p_operation_id, 'admission-projection'
    );
    projection_hash := memory_private.projection_manifest_sha256(
      actor, resulting_claim_id, resulting_revision_id,
      projection_operation_id, 'upsert', resulting_sequence,
      revision_hash, proposal.selection_binding_sha256,
      retrieval_hash, retrieval_hash
    );
    INSERT INTO memory.projection_outbox(
      outbox_id, owner_user_id, claim_id, revision_id, operation_id,
      sequence_number, operation, point_id, revision_sha256,
      selection_binding_sha256, projection_manifest_sha256
    ) VALUES (
      resulting_outbox_id, actor, resulting_claim_id,
      resulting_revision_id, projection_operation_id,
      resulting_sequence, 'upsert', resulting_claim_id,
      revision_hash, proposal.selection_binding_sha256, projection_hash
    );
  END IF;

  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, prior_state_sha256,
    new_state_sha256, reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id,
    CASE WHEN automatic_admission THEN 'system' ELSE 'reviewer' END,
    CASE WHEN automatic_admission THEN NULL::uuid ELSE actor END, 'claim',
    resulting_claim_id, 'proposal_admitted', prior_state_hash,
    new_state_hash, 'proposal_admitted', event_hash
  );
  RETURN QUERY SELECT 'admitted'::text, resulting_claim_id,
    resulting_revision_id, resulting_outbox_id;
END;
$function$;

CREATE FUNCTION memory_private.auto_admit_one_ordinary_proposal()
RETURNS TABLE(
  outcome text,
  proposal_id uuid,
  claim_id uuid,
  revision_id uuid,
  outbox_id uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  SELECT proposal.proposal_id, proposal.owner_user_id,
    proposal.operation_id, proposal.proposal_sha256,
    evidence.source_sha256, proposal.selected_sha256,
    proposal.selection_binding_sha256, proposal.predicate_catalog_sha256
  INTO candidate
  FROM memory.proposal AS proposal
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = proposal.owner_user_id
   AND evidence.evidence_id = proposal.evidence_id
  JOIN memory.provider_call AS provider_call
    ON provider_call.owner_user_id = proposal.owner_user_id
   AND provider_call.provider_call_id = proposal.provider_call_id
  JOIN memory.extraction_job AS extraction_job
    ON extraction_job.owner_user_id = proposal.owner_user_id
   AND extraction_job.job_id = provider_call.job_id
   AND extraction_job.evidence_id = proposal.evidence_id
  WHERE proposal.review_state = 'pending_review'
    AND proposal.proposal_purpose = 'new_claim'
    AND proposal.correction_of_claim_id IS NULL
    AND proposal.subject_entity_type = 'self'
    AND proposal.subject_entity_key = 'self'
    AND proposal.epistemic_state = 'supported'
    AND proposal.sensitivity = 'ordinary'
    AND proposal.projectable
    AND proposal.surface = 'normal'
    AND NOT proposal.requires_explicit
    AND pg_catalog.cardinality(proposal.domains) = 0
    AND pg_catalog.cardinality(proposal.intents) = 0
    AND proposal.valid_from IS NULL AND proposal.valid_to IS NULL
    AND proposal.expires_at > pg_catalog.transaction_timestamp()
    AND evidence.source_kind = 'conversation_message'
    AND evidence.eligibility_decision = 'send_external'
    AND provider_call.state = 'completed'
    AND provider_call.error_code IS NULL
    AND extraction_job.state = 'completed'
    AND NOT memory_private.owner_source_erasure_active(proposal.owner_user_id)
    AND EXISTS (
      SELECT 1 FROM memory.pilot_marker AS marker
      WHERE marker.pilot_ever_started
        AND evidence.source_created_at >= marker.started_at
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.claim AS existing_claim
      WHERE existing_claim.owner_user_id = proposal.owner_user_id
        AND existing_claim.semantic_key_sha256 = proposal.semantic_key_sha256
    )
  ORDER BY proposal.created_at, proposal.proposal_id
  LIMIT 1;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  PERFORM pg_catalog.set_config(
    'app.user_id', candidate.owner_user_id::text, true
  );
  PERFORM pg_catalog.set_config(
    'app.auth_context_sha256',
    '421ec318b932459828e25f2ed52fc3cc589bd09ee333fb4d77dbbddf61450432',
    true
  );
  RETURN QUERY
  SELECT reviewed.outcome, candidate.proposal_id,
    reviewed.claim_id, reviewed.revision_id, reviewed.outbox_id
  FROM memory_private.review_proposal(
    candidate.operation_id, candidate.proposal_id, 'admitted',
    candidate.proposal_sha256, candidate.source_sha256,
    candidate.selected_sha256, candidate.selection_binding_sha256,
    candidate.predicate_catalog_sha256,
    ARRAY['automatic_low_risk_owner_assertion']::text[]
  ) AS reviewed;
END;
$function$;
ALTER FUNCTION memory_private.auto_admit_one_ordinary_proposal()
  OWNER TO governed_memory_owner;
REVOKE ALL ON FUNCTION memory_private.auto_admit_one_ordinary_proposal()
  FROM PUBLIC, governed_memory_api;
GRANT EXECUTE ON FUNCTION memory_private.auto_admit_one_ordinary_proposal()
  TO governed_memory_worker;
GRANT EXECUTE ON FUNCTION memory_private.review_proposal(
  uuid,uuid,text,text,text,text,text,text,text[]
) TO governed_memory_worker;

DO $postflight$
BEGIN
  IF pg_catalog.to_regprocedure(
       'memory_private.auto_admit_one_ordinary_proposal()'
     ) IS NULL
     OR NOT pg_catalog.has_function_privilege(
       'governed_memory_worker',
       'memory_private.auto_admit_one_ordinary_proposal()', 'EXECUTE'
     )
     OR NOT pg_catalog.has_function_privilege(
       'governed_memory_worker',
       'memory_private.review_proposal(uuid,uuid,text,text,text,text,text,text,text[])',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'bounded automatic admission postflight failed';
  END IF;
END;
$postflight$;
RESET ROLE;
