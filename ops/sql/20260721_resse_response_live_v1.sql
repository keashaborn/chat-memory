BEGIN;

CREATE OR REPLACE FUNCTION memory.read_governed_claims_v1(p_claim_ids uuid[])
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  revision_id uuid,
  canonical_key text,
  canonical_text text,
  predicate text,
  status text,
  sensitivity text,
  importance numeric,
  salience numeric,
  valid_from timestamptz,
  valid_to timestamptz,
  superseded_by uuid,
  metadata jsonb,
  retrieval_policy jsonb,
  projection_review_decision text,
  projection_apply_outcome text,
  evidence_by_stance jsonb,
  observation_ids text[],
  project_key text,
  component_key text,
  source_content_sha256 text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
  SELECT source.owner_user_id,
         source.claim_id,
         NULL::uuid AS revision_id,
         source.canonical_key,
         source.canonical_text,
         source.predicate,
         source.status,
         source.sensitivity,
         source.importance,
         source.salience,
         source.valid_from,
         source.valid_to,
         NULL::uuid AS superseded_by,
         source.metadata,
         source.retrieval_policy,
         source.projection_review_decision,
         source.projection_apply_outcome,
         source.evidence_by_stance,
         source.observation_ids,
         source.project_key,
         NULL::text AS component_key,
         encode(
           digest(
             jsonb_build_object(
               'canonical_key',source.canonical_key,
               'canonical_text',source.canonical_text,
               'predicate',source.predicate,
               'status',source.status,
               'sensitivity',source.sensitivity,
               'valid_from',source.valid_from,
               'valid_to',source.valid_to,
               'metadata',source.metadata,
               'retrieval_policy',source.retrieval_policy
             )::text,
             'sha256'
           ),
           'hex'
         ) AS source_content_sha256
    FROM memory.read_v5_shadow_claims(p_claim_ids) AS source
   ORDER BY source.claim_id;
$function$;

ALTER FUNCTION memory.read_governed_claims_v1(uuid[]) OWNER TO memory_v5_reader;
REVOKE ALL ON FUNCTION memory.read_governed_claims_v1(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.read_governed_claims_v1(uuid[]) TO brains_app;

CREATE TABLE memory.assistant_transcript_attestation_v1 (
  answer_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  chat_log_id uuid NOT NULL UNIQUE,
  request_id_sha256 text NOT NULL CHECK (request_id_sha256 ~ '^[0-9a-f]{64}$'),
  conversation_snapshot_sha256 text NOT NULL CHECK (conversation_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  trusted_plan_sha256 text NOT NULL CHECK (trusted_plan_sha256 ~ '^[0-9a-f]{64}$'),
  provider_request_sha256 text NOT NULL CHECK (provider_request_sha256 ~ '^[0-9a-f]{64}$'),
  provider_response_sha256 text NOT NULL CHECK (provider_response_sha256 ~ '^[0-9a-f]{64}$'),
  provider_response_id text NOT NULL CHECK (length(provider_response_id) BETWEEN 1 AND 240),
  output_kind text NOT NULL CHECK (output_kind IN ('content','refusal')),
  assistant_text_sha256 text NOT NULL CHECK (assistant_text_sha256 ~ '^[0-9a-f]{64}$'),
  attestation_sha256 text NOT NULL UNIQUE CHECK (attestation_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL,
  CHECK (answer_id=chat_log_id)
);

CREATE INDEX assistant_transcript_attestation_owner_thread_created_idx
  ON memory.assistant_transcript_attestation_v1(owner_user_id,thread_id,created_at DESC,answer_id DESC);

ALTER TABLE memory.assistant_transcript_attestation_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.assistant_transcript_attestation_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY assistant_transcript_attestation_owner_policy
  ON memory.assistant_transcript_attestation_v1
  USING (owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid)
  WITH CHECK (owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid);

CREATE TABLE memory.final_answer_memory_binding_v1 (
  answer_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  binding_manifest_sha256 text NOT NULL UNIQUE CHECK (binding_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  binding jsonb NOT NULL CHECK (jsonb_typeof(binding)='object'),
  created_at timestamptz NOT NULL
);

CREATE INDEX final_answer_memory_binding_owner_thread_created_idx
  ON memory.final_answer_memory_binding_v1(owner_user_id,thread_id,created_at DESC,answer_id DESC);

ALTER TABLE memory.final_answer_memory_binding_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.final_answer_memory_binding_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY final_answer_memory_binding_owner_policy
  ON memory.final_answer_memory_binding_v1
  USING (owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid)
  WITH CHECK (owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid);

ALTER TABLE memory.assistant_transcript_attestation_v1 OWNER TO sage;
ALTER TABLE memory.final_answer_memory_binding_v1 OWNER TO sage;
REVOKE ALL ON memory.assistant_transcript_attestation_v1,
              memory.final_answer_memory_binding_v1 FROM PUBLIC,brains_app;
GRANT SELECT,INSERT ON memory.assistant_transcript_attestation_v1,
                       memory.final_answer_memory_binding_v1 TO brains_app;

COMMENT ON TABLE memory.assistant_transcript_attestation_v1 IS
  'Append-only backend proof for assistant transcript rows admitted to prompt history.';
COMMENT ON TABLE memory.final_answer_memory_binding_v1 IS
  'Append-only exact governed-memory selection/injection/exposure binding for a final answer.';

COMMIT;
