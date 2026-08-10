-- Empty-only rollback for governed_memory_foundation_0001.
-- Run only in `governed_memory` as governed_memory_owner. The migration runner
-- supplies BEGIN/COMMIT, timeouts, and the same advisory transaction lock used
-- for the forward migration. No owner data is removed by this rollback.

DO $preflight$
BEGIN
  IF current_database() <> 'governed_memory'
     OR current_user <> 'governed_memory_owner' THEN
    RAISE EXCEPTION
      'foundation rollback requires governed_memory_owner in governed_memory';
  END IF;
  IF EXISTS (SELECT 1 FROM memory.evidence LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.extraction_job LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.provider_call LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.proposal LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.entity LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.claim LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.claim_revision LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.claim_evidence LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.projection_outbox LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.answer_binding LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.audit_event LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.claim_deletion_receipt LIMIT 1) THEN
    RAISE EXCEPTION 'foundation rollback is empty-only; owner data exists';
  END IF;
END;
$preflight$;

DROP FUNCTION memory_private.record_answer_binding(
  uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,text,text,
  boolean,uuid[],uuid[],text,text,text,text,text
);
DROP FUNCTION memory_private.finalize_claim_deletion(
  uuid,uuid,uuid,text,uuid,uuid,text,integer,text,text,
  timestamptz,timestamptz,text,text
);
DROP FUNCTION memory_private.finish_projection_job(
  uuid,uuid,text,text,text,text,text,text
);
DROP FUNCTION memory_private.lease_projection_jobs(text,integer,integer);
DROP FUNCTION memory_private.request_claim_deletion(uuid,uuid,text,text);
DROP FUNCTION memory_private.retract_claim(uuid,uuid,text,text);
DROP FUNCTION memory_private.reject_pending_correction_for_lifecycle(
  uuid,uuid,uuid
);
DROP FUNCTION memory_private.correct_claim(uuid,uuid,text,text,text,jsonb);
DROP FUNCTION memory_private.redact_expired_evidence_excerpts(integer);
DROP FUNCTION memory_private.purge_expired_answer_bindings(integer);
DROP FUNCTION memory_private.purge_terminal_proposals(integer);
DROP FUNCTION memory_private.expire_proposals(integer);
DROP FUNCTION memory_private.review_proposal(
  uuid,uuid,text,text,text,text,text,text,text[]
);
DROP FUNCTION memory_private.read_status();
DROP FUNCTION memory_private.read_operation(uuid);
DROP FUNCTION memory_private.list_claims(uuid,integer,timestamptz,uuid);
DROP FUNCTION memory_private.read_claim_candidates(uuid[]);
DROP FUNCTION memory_private.list_proposals(integer,timestamptz,uuid);
DROP FUNCTION memory_private.complete_extraction(
  uuid,uuid,uuid,text,text,text,integer,integer,text,jsonb
);
DROP FUNCTION memory_private.mark_provider_call_dispatched(
  uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION memory_private.lease_extraction_jobs(
  text,integer,integer,text,text,text,text,text,text,integer,integer
);
DROP FUNCTION memory_private.read_ingest_receipt(uuid,uuid,text);
DROP FUNCTION memory_private.record_selected_evidence(
  uuid,uuid,text,uuid,uuid,uuid,text,text,text,integer,integer,uuid,text,
  timestamptz,text,text,text
);
DROP FUNCTION memory_private.operation_id_conflicts(
  uuid,uuid,uuid,uuid,uuid,text[]
);

DROP TRIGGER evidence_guard ON memory.evidence;
DROP TRIGGER provider_call_guard ON memory.provider_call;
DROP TRIGGER proposal_guard ON memory.proposal;
DROP TRIGGER projection_outbox_guard ON memory.projection_outbox;
DROP TRIGGER claim_revision_immutable ON memory.claim_revision;
DROP TRIGGER claim_evidence_immutable ON memory.claim_evidence;
DROP TRIGGER answer_binding_immutable ON memory.answer_binding;
DROP TRIGGER audit_event_append_only ON memory.audit_event;
DROP TRIGGER claim_deletion_receipt_immutable
  ON memory.claim_deletion_receipt;

DROP FUNCTION memory_private.guard_projection_outbox_mutation();
DROP FUNCTION memory_private.guard_proposal_mutation();
DROP FUNCTION memory_private.guard_provider_call_mutation();
DROP FUNCTION memory_private.guard_evidence_mutation();
DROP FUNCTION memory_private.guard_append_only_audit();
DROP FUNCTION memory_private.guard_answer_binding_mutation();
DROP FUNCTION memory_private.guard_immutable_fact();

ALTER TABLE memory.proposal
  DROP CONSTRAINT proposal_correction_revision_fk;
ALTER TABLE memory.proposal
  DROP CONSTRAINT proposal_correction_claim_fk;
ALTER TABLE memory.claim
  DROP CONSTRAINT claim_current_revision_fk;

DROP TABLE memory.answer_binding;
DROP TABLE memory.claim_deletion_receipt;
DROP TABLE memory.claim_evidence;
DROP TABLE memory.projection_outbox;
DROP TABLE memory.claim_revision;
DROP TABLE memory.proposal;
DROP TABLE memory.claim;
DROP TABLE memory.entity;
DROP TABLE memory.provider_call;
DROP TABLE memory.extraction_job;
DROP TABLE memory.evidence;
DROP TABLE memory.audit_event;
DROP TABLE memory.predicate_catalog;

DROP FUNCTION memory_private.answer_injection_manifest_sha256(
  text,uuid[],uuid[],text[],text,integer,text,integer,integer,integer,text
);
DROP FUNCTION memory_private.answer_selection_manifest_sha256(
  uuid,uuid,text,text,text,text,boolean,uuid[],uuid[],text[],text
);
DROP FUNCTION memory_private.answer_renderer_sha256();
DROP FUNCTION memory_private.render_answer_memory_record(
  text,text,text,text,text,text,text,text
);
DROP FUNCTION memory_private.retrieval_policy_sha256(
  boolean,text[],text[],text[],integer,integer
);
DROP FUNCTION memory_private.framed_text_array(text,text[]);

DROP FUNCTION memory_private.proposal_sha256(
  uuid,uuid,uuid,uuid,text,uuid,uuid,uuid,text,text,text,text,uuid,text,
  text,text,text,text,uuid,uuid,integer,text,text,text,integer,text,smallint,
  uuid,uuid,text,text,text,text,text,text,text,text,jsonb,text,text,text,
  boolean,text[],text[],text,boolean,timestamptz,timestamptz
);
DROP FUNCTION memory_private.projection_manifest_sha256(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
);
DROP FUNCTION memory_private.projection_contract_sha256();
DROP FUNCTION memory_private.claim_state_sha256(
  uuid,uuid,text,text,text,boolean,uuid,integer,text,integer
);
DROP FUNCTION memory_private.claim_revision_sha256(
  uuid,uuid,uuid,integer,uuid,text,text,text,text,text,text,text,text,text,
  text,text,jsonb,text,text,boolean,text[],text[],text,boolean,timestamptz,
  timestamptz,text,text,text,text,text
);
DROP FUNCTION memory_private.render_relational_fact(
  text,text,text,text,text,text,text,text,jsonb
);
DROP FUNCTION memory_private.revision_fact_policy_sha256(
  text,text,text,text,text,text,text,text,jsonb,text,text,boolean,text[],text[],
  text,boolean,timestamptz,timestamptz,text,text,text,text
);
DROP FUNCTION memory_private.correction_source_material(
  text,text,text,text,text,jsonb,text,text
);
DROP FUNCTION memory_private.source_local_entity_key(
  uuid,text,integer,text,text
);
DROP FUNCTION memory_private.claim_identity_sha256(text,text);
DROP FUNCTION memory_private.semantic_key_sha256(text,text,text,text,jsonb);
DROP FUNCTION memory_private.selection_binding_sha256(
  uuid,text,uuid,uuid,uuid,text,text,text,integer,integer,uuid,text
);
DROP FUNCTION memory_private.ingest_successor_receipt_sha256(
  uuid,uuid,text,text,uuid,uuid
);
DROP FUNCTION memory_private.correction_window_sha256(
  uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
);
DROP FUNCTION memory_private.validated_fact_sha256(
  text,integer,text,text,text,text,text,text,text,text,jsonb,text,text,text
);
DROP FUNCTION memory_private.uuid5(uuid,text);
DROP FUNCTION memory_private.derived_uuid(uuid,text);
DROP FUNCTION memory_private.answer_binding_retention_receipt_sha256(
  uuid,uuid,uuid,timestamptz
);
DROP FUNCTION memory_private.timestamp_utc_text(timestamptz);
DROP FUNCTION memory_private.timestamp_epoch_us(timestamptz);
DROP FUNCTION memory_private.framed_utf8_field(text,text);
DROP FUNCTION memory_private.is_ascii_key_array(text[]);
DROP FUNCTION memory_private.is_sorted_unique_key_array(
  text[],integer,integer
);
DROP FUNCTION memory_private.is_sorted_unique_allowlist(text[],text[]);
DROP FUNCTION memory_private.current_owner_id();

DROP SCHEMA memory_private;
DROP SCHEMA memory;
