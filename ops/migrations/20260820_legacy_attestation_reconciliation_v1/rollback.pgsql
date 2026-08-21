
BEGIN;

DO $preflight$
DECLARE
  exact_rows integer;
BEGIN
  IF current_user <> 'sage' OR current_database() <> 'memory' THEN
    RAISE EXCEPTION 'legacy attestation rollback target mismatch';
  END IF;
  IF COALESCE(current_setting('lifeswitch.legacy_attestation_receipt_sha256', true), '') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'hash-bound reconciliation receipt is required';
  END IF;
  SELECT count(*)::integer INTO exact_rows
  FROM memory.assistant_transcript_attestation_v1 AS a
  JOIN chat_integrity.assistant_transcript_attestation_v1 AS c
    ON c.answer_id = a.answer_id
   AND c.attestation_sha256 = a.attestation_sha256;
  IF exact_rows <> 32 THEN
    RAISE EXCEPTION 'legacy attestation rollback row set drifted: %', exact_rows;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('legacy_attestation_reconciliation_v1', 0));
END;
$preflight$;

DELETE FROM chat_integrity.assistant_transcript_attestation_v1 AS c
USING memory.assistant_transcript_attestation_v1 AS a
WHERE c.answer_id = a.answer_id
  AND c.attestation_sha256 = a.attestation_sha256;

COMMIT;
