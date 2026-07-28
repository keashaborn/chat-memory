BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'contextual intake v2 rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.evidence_contextual_span_v2 LIMIT 1
  ) THEN
    RAISE EXCEPTION 'contextual intake v2 rollback refuses durable rows';
  END IF;
END
$preflight$;

DROP FUNCTION IF EXISTS memory.finalize_owner_contextual_split_v2(
  uuid,text,text,text,integer
);
DROP FUNCTION IF EXISTS memory.apply_owner_contextual_split_v2(
  uuid,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_contextual_split_v2(
  uuid,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.plan_owner_evidence_intake_v2(
  text,integer,uuid
);
DROP TABLE IF EXISTS memory.evidence_contextual_span_v2;

REVOKE EXECUTE ON FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  FROM memory_context_rebind_maintainer;
REVOKE EXECUTE ON FUNCTION memory.atomic_span_uuid_v1(text)
  FROM memory_context_rebind_maintainer;
REVOKE EXECUTE ON FUNCTION memory.atomic_span_uuid_v1(text)
  FROM memory_intake_maintainer;

ALTER TABLE memory.evidence_intake_terminal
  DROP CONSTRAINT evidence_intake_terminal_reason_code_check;
ALTER TABLE memory.evidence_intake_terminal
  ADD CONSTRAINT evidence_intake_terminal_reason_code_check
  CHECK (
    reason_code IN (
      'empty_content',
      'missing_content_hash',
      'upstream_completed_empty',
      'upstream_skipped',
      'upstream_review_required',
      'upstream_link_inconsistency',
      'eligible_dispatched'
    )
  );

COMMIT;
