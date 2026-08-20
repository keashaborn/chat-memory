BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '15s';
SELECT pg_catalog.pg_advisory_xact_lock(724613048320);

DO $migration$
BEGIN
  IF pg_catalog.to_regclass('lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1') IS NULL THEN
    RAISE EXCEPTION 'required prior-answer provenance view is absent';
  END IF;
END
$migration$;

CREATE OR REPLACE VIEW lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1
WITH (security_barrier=true,security_invoker=false)
AS
SELECT
  c.context_id,
  b.owner_user_id,
  b.thread_id,
  b.answer_id,
  b.authenticated_actor_user_id AS binding_actor_user_id,
  b.request_id_sha256 AS binding_request_id_sha256,
  b.conversation_snapshot_sha256 AS binding_snapshot_sha256,
  b.source_assembly_sha256 AS binding_source_assembly_sha256,
  b.envelope_sha256 AS binding_envelope_sha256,
  b.rendered_content_sha256 AS binding_rendered_content_sha256,
  b.answer_model_exposed AS binding_answer_model_exposed,
  b.record_count AS binding_record_count,
  b.rendered_tokens AS binding_rendered_tokens,
  b.record_refs AS binding_record_refs,
  b.binding_manifest_sha256,
  b.created_at,
  a.answer_id AS attestation_answer_id,
  a.request_id_sha256 AS attestation_request_id_sha256,
  a.conversation_snapshot_sha256 AS attestation_snapshot_sha256,
  a.assistant_text_sha256 AS attestation_assistant_text_sha256,
  a.attestation_sha256,
  a.created_at AS attestation_created_at,
  l.id AS chat_log_id,
  l.source AS chat_source,
  r.authenticated_actor_user_id AS receipt_actor_user_id,
  r.request_id_sha256 AS receipt_request_id_sha256,
  r.conversation_snapshot_sha256 AS receipt_snapshot_sha256,
  r.lifeswitch_prepared_context_manifest_sha256 AS prepared_context_manifest_sha256,
  r.data_plan_sha256,
  r.lifeswitch_binding_manifest_sha256 AS receipt_binding_manifest_sha256,
  r.source_assembly_sha256 AS receipt_source_assembly_sha256,
  r.envelope_sha256 AS receipt_envelope_sha256,
  r.assistant_text_sha256 AS receipt_assistant_text_sha256,
  r.attestation_sha256 AS receipt_attestation_sha256,
  r.answer_model_exposed AS receipt_answer_model_exposed,
  r.source_refs AS receipt_source_refs,
  r.created_at AS receipt_created_at,
  r.receipt_manifest_sha256
FROM lifeswitch_chat.owner_read_context_v1 c
JOIN lifeswitch_chat.final_answer_lifeswitch_binding_v1 b
  ON b.owner_user_id=c.owner_user_id
 AND b.thread_id=c.thread_id
JOIN memory.assistant_transcript_attestation_v1 a
  ON a.owner_user_id=b.owner_user_id
 AND a.thread_id=b.thread_id
 AND a.answer_id=b.answer_id
 AND a.request_id_sha256=b.request_id_sha256
 AND a.conversation_snapshot_sha256=b.conversation_snapshot_sha256
 AND a.created_at=b.created_at
JOIN public.chat_log l
  ON l.owner_user_id=b.owner_user_id
 AND l.thread_id=b.thread_id
 AND l.id=b.answer_id
 AND l.source IN ('backend/resse:assistant:v1','backend/seebx:assistant:v2')
 AND pg_catalog.encode(public.digest(l.text,'sha256'),'hex')=a.assistant_text_sha256
LEFT JOIN lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 r
  ON r.owner_user_id=b.owner_user_id
 AND r.thread_id=b.thread_id
 AND r.answer_id=b.answer_id
WHERE c.backend_pid=pg_catalog.pg_backend_pid()
  AND c.expires_at>pg_catalog.clock_timestamp()
  AND c.owner_user_id=NULLIF(pg_catalog.current_setting('app.user_id',true),'')::uuid
  AND c.owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid;

ALTER VIEW lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 OWNER TO sage;
REVOKE ALL ON lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 FROM PUBLIC;
GRANT SELECT ON lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 TO lifeswitch_chat_reader_v1;
COMMENT ON VIEW lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 IS
  'Owner-scoped prior LifeSwitch answer provenance; accepts exact legacy and canonical SeeBx assistant sources.';
COMMIT;
