
BEGIN;

DO $preflight$
DECLARE
  source_rows integer;
  eligible_rows integer;
  orphan_rows integer;
  destination_conflicts integer;
BEGIN
  IF current_user <> 'sage' OR current_database() <> 'memory' THEN
    RAISE EXCEPTION 'legacy attestation reconciliation target mismatch';
  END IF;
  IF COALESCE(current_setting('lifeswitch.legacy_attestation_receipt_sha256', true), '') !~ '^[0-9a-f]{64}$'
     OR COALESCE(current_setting('lifeswitch.legacy_attestation_quarantine_ciphertext_sha256', true), '') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'hash-bound encrypted quarantine receipt is required';
  END IF;
  IF COALESCE(current_setting('lifeswitch.legacy_attestation_source_sha256', true), '') <> 'c07967305707d83e258bd441939359b472b95bb414eeb739a7edd99a5f657831'
     OR COALESCE(current_setting('lifeswitch.legacy_attestation_eligible_sha256', true), '') <> '4b3917cac82fc5a4522a2bb8e0a9296651daedc3d94fb6b02840d9b5f6b64678'
     OR COALESCE(current_setting('lifeswitch.legacy_attestation_quarantine_sha256', true), '') <> '38f20b7df5cbd1d3a8d413825eb76a6a7a682f0e78b491e1aa09a315113d90e1' THEN
    RAISE EXCEPTION 'legacy attestation source classification hash mismatch';
  END IF;

  SELECT count(*)::integer INTO source_rows
  FROM memory.assistant_transcript_attestation_v1;
  SELECT count(*)::integer INTO eligible_rows
  FROM memory.assistant_transcript_attestation_v1 AS a
  JOIN public.chat_log AS l
    ON l.id = a.answer_id
   AND l.id = a.chat_log_id
   AND l.owner_user_id = a.owner_user_id
   AND l.thread_id = a.thread_id
   AND pg_catalog.encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256;
  orphan_rows := source_rows - eligible_rows;
  IF source_rows <> 190 OR eligible_rows <> 32 OR orphan_rows <> 158 THEN
    RAISE EXCEPTION 'legacy attestation classification drifted: source %, eligible %, orphan %', source_rows, eligible_rows, orphan_rows;
  END IF;

  SELECT count(*)::integer INTO destination_conflicts
  FROM memory.assistant_transcript_attestation_v1 AS a
  JOIN chat_integrity.assistant_transcript_attestation_v1 AS c
    ON c.answer_id = a.answer_id;
  IF destination_conflicts <> 0 THEN
    RAISE EXCEPTION 'canonical attestation conflicts exist: %', destination_conflicts;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('legacy_attestation_reconciliation_v1', 0));
END;
$preflight$;

INSERT INTO chat_integrity.assistant_transcript_attestation_v1 (
  answer_id, owner_user_id, thread_id, chat_log_id, request_id_sha256,
  conversation_snapshot_sha256, trusted_plan_sha256,
  provider_request_sha256, provider_response_sha256, provider_response_id,
  output_kind, assistant_text_sha256, attestation_sha256, created_at
)
SELECT
  a.answer_id, a.owner_user_id, a.thread_id, a.chat_log_id, a.request_id_sha256,
  a.conversation_snapshot_sha256, a.trusted_plan_sha256,
  a.provider_request_sha256, a.provider_response_sha256, a.provider_response_id,
  a.output_kind, a.assistant_text_sha256, a.attestation_sha256, a.created_at
FROM memory.assistant_transcript_attestation_v1 AS a
JOIN public.chat_log AS l
  ON l.id = a.answer_id
 AND l.id = a.chat_log_id
 AND l.owner_user_id = a.owner_user_id
 AND l.thread_id = a.thread_id
 AND pg_catalog.encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256
ORDER BY a.answer_id;

DO $postcondition$
DECLARE
  exact_rows integer;
BEGIN
  SELECT count(*)::integer INTO exact_rows
  FROM memory.assistant_transcript_attestation_v1 AS a
  JOIN public.chat_log AS l
    ON l.id = a.answer_id
   AND l.id = a.chat_log_id
   AND l.owner_user_id = a.owner_user_id
   AND l.thread_id = a.thread_id
   AND pg_catalog.encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256
  JOIN chat_integrity.assistant_transcript_attestation_v1 AS c
    ON (c.answer_id, c.owner_user_id, c.thread_id, c.chat_log_id,
        c.request_id_sha256, c.conversation_snapshot_sha256,
        c.trusted_plan_sha256, c.provider_request_sha256,
        c.provider_response_sha256, c.provider_response_id, c.output_kind,
        c.assistant_text_sha256, c.attestation_sha256, c.created_at)
     = (a.answer_id, a.owner_user_id, a.thread_id, a.chat_log_id,
        a.request_id_sha256, a.conversation_snapshot_sha256,
        a.trusted_plan_sha256, a.provider_request_sha256,
        a.provider_response_sha256, a.provider_response_id, a.output_kind,
        a.assistant_text_sha256, a.attestation_sha256, a.created_at);
  IF exact_rows <> 32 THEN
    RAISE EXCEPTION 'canonical attestation reconciliation mismatch: %', exact_rows;
  END IF;
END;
$postcondition$;

COMMIT;
