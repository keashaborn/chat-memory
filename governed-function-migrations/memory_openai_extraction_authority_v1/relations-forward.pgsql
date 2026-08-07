ALTER TABLE memory.evidence_extraction_job FORCE ROW LEVEL SECURITY;
CREATE TABLE memory.openai_provider_request_receipt_v1 (
  receipt_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  run_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  request_receipt_sha256 text NOT NULL CHECK (request_receipt_sha256 ~ '^[0-9a-f]{64}$'),
  request_receipt jsonb NOT NULL CHECK (
    jsonb_typeof(request_receipt)='object' AND pg_column_size(request_receipt)<=16384
  ),
  provider_id text NOT NULL CHECK (provider_id='openai_responses'),
  provider_version text NOT NULL CHECK (provider_version='v1'),
  provider_model_sha256 text NOT NULL CHECK (provider_model_sha256 ~ '^[0-9a-f]{64}$'),
  worker_id_sha256 text NOT NULL CHECK (worker_id_sha256 ~ '^[0-9a-f]{64}$'),
  maximum_cost_microusd bigint NOT NULL CHECK (maximum_cost_microusd>0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,job_id,request_sha256),
  UNIQUE(owner_user_id,receipt_id),
  FOREIGN KEY(owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id),
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
);
CREATE INDEX openai_provider_request_receipt_v1_owner_time_idx
  ON memory.openai_provider_request_receipt_v1(
    owner_user_id,created_at,receipt_id
  );
ALTER TABLE memory.openai_provider_request_receipt_v1 OWNER TO sage;
ALTER TABLE memory.openai_provider_request_receipt_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.openai_provider_request_receipt_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.openai_provider_request_receipt_v1
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
GRANT SELECT,INSERT ON memory.openai_provider_request_receipt_v1
  TO memory_v5_extraction_scheduler_maintainer;

CREATE TABLE memory.openai_provider_completion_receipt_v1 (
  receipt_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  run_id uuid NOT NULL,
  job_id uuid NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('accepted','rejected')),
  completion_audit_sha256 text NOT NULL CHECK (
    completion_audit_sha256 ~ '^[0-9a-f]{64}$'
  ),
  completion_audit jsonb NOT NULL CHECK (
    jsonb_typeof(completion_audit)='object'
    AND pg_column_size(completion_audit)<=24576
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  FOREIGN KEY(owner_user_id,receipt_id)
    REFERENCES memory.openai_provider_request_receipt_v1(owner_user_id,receipt_id),
  FOREIGN KEY(owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
);
CREATE INDEX openai_provider_completion_receipt_v1_owner_time_idx
  ON memory.openai_provider_completion_receipt_v1(
    owner_user_id,created_at,receipt_id
  );
ALTER TABLE memory.openai_provider_completion_receipt_v1 OWNER TO sage;
ALTER TABLE memory.openai_provider_completion_receipt_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.openai_provider_completion_receipt_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.openai_provider_completion_receipt_v1
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
GRANT SELECT,INSERT ON memory.openai_provider_completion_receipt_v1
  TO memory_v5_extraction_scheduler_maintainer;
