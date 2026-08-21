
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
