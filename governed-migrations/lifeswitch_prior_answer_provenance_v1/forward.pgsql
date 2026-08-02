CREATE TABLE lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 (
  answer_id uuid PRIMARY KEY,
  authenticated_actor_user_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  request_id_sha256 text NOT NULL CHECK (request_id_sha256 ~ '^[0-9a-f]{64}$'),
  conversation_snapshot_sha256 text NOT NULL CHECK (conversation_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  lifeswitch_prepared_context_manifest_sha256 text NOT NULL CHECK (lifeswitch_prepared_context_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  data_plan_sha256 text NOT NULL CHECK (data_plan_sha256 ~ '^[0-9a-f]{64}$'),
  lifeswitch_binding_manifest_sha256 text NOT NULL CHECK (lifeswitch_binding_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  source_assembly_sha256 text NOT NULL CHECK (source_assembly_sha256 ~ '^[0-9a-f]{64}$'),
  envelope_sha256 text NOT NULL CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'),
  assistant_text_sha256 text NOT NULL CHECK (assistant_text_sha256 ~ '^[0-9a-f]{64}$'),
  attestation_sha256 text NOT NULL CHECK (attestation_sha256 ~ '^[0-9a-f]{64}$'),
  answer_model_exposed boolean NOT NULL CHECK (answer_model_exposed),
  source_refs jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(source_refs)='array'),
  created_at timestamptz NOT NULL,
  persisted_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  receipt_manifest_sha256 text NOT NULL UNIQUE CHECK (receipt_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (authenticated_actor_user_id=owner_user_id)
);
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 OWNER TO sage;
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY final_answer_lifeswitch_provenance_writer_v1 ON lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 FOR ALL TO lifeswitch_chat_binding_writer_v1 USING (owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid) WITH CHECK (owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid);
CREATE INDEX final_answer_lifeswitch_provenance_owner_thread_v1 ON lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1(owner_user_id,thread_id,created_at DESC,answer_id DESC);
GRANT SELECT,INSERT ON lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 TO lifeswitch_chat_binding_writer_v1;
CREATE VIEW lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1
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
 AND l.source='backend/resse:assistant:v1'
 AND pg_catalog.encode(public.digest(l.text,'sha256'),'hex')=a.assistant_text_sha256
LEFT JOIN lifeswitch_chat.final_answer_lifeswitch_provenance_receipt_v1 r
  ON r.owner_user_id=b.owner_user_id
 AND r.thread_id=b.thread_id
 AND r.answer_id=b.answer_id
WHERE c.backend_pid=pg_catalog.pg_backend_pid()
  AND c.expires_at>pg_catalog.clock_timestamp()
  AND c.owner_user_id=NULLIF(pg_catalog.current_setting('app.user_id',true),'')::uuid
  AND c.owner_user_id=NULLIF(pg_catalog.current_setting('app.lifeswitch_owner_id',true),'')::uuid;
GRANT SELECT ON lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1 TO lifeswitch_chat_reader_v1;
