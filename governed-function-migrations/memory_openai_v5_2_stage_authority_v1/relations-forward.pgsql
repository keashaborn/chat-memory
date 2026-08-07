CREATE TABLE memory.v5_2_openai_packet_route_event (
  route_event_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  route text NOT NULL CHECK (route='manual_review_artifact_ready'),
  reason_code text NOT NULL CHECK (reason_code='reviewable_relational_packet_v5_2'),
  routing_basis_sha256 text NOT NULL CHECK (routing_basis_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_content_sha256 text NOT NULL CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  validator_packet_sha256 text NOT NULL CHECK (validator_packet_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  entity_mention_count smallint NOT NULL CHECK (entity_mention_count BETWEEN 0 AND 24),
  observation_count smallint NOT NULL CHECK (observation_count BETWEEN 0 AND 32),
  comparison_hint_count smallint NOT NULL CHECK (comparison_hint_count BETWEEN 0 AND 32),
  deferral_count smallint NOT NULL CHECK (deferral_count BETWEEN 0 AND 32),
  source_deferral_reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[] CHECK (cardinality(source_deferral_reason_codes)=0),
  review_id uuid NOT NULL,
  request_id uuid NOT NULL,
  review_contract text NOT NULL CHECK (review_contract='memory_v1_v5_2_openai_packet_review_v1'),
  bundle_contract text NOT NULL CHECK (bundle_contract='memory_v1_v5_2_stage_preflight_v1'),
  review_report_sha256 text NOT NULL CHECK (review_report_sha256 ~ '^[0-9a-f]{64}$'),
  stage_bundle_sha256 text NOT NULL CHECK (stage_bundle_sha256 ~ '^[0-9a-f]{64}$'),
  review_report jsonb NOT NULL CHECK (
    jsonb_typeof(review_report)='object' AND pg_column_size(review_report)<=196608
  ),
  stage_bundle jsonb NOT NULL CHECK (
    jsonb_typeof(stage_bundle)='object' AND pg_column_size(stage_bundle)<=262144
  ),
  repository_commit text NOT NULL CHECK (repository_commit ~ '^[0-9a-f]{40}$'),
  auto_link_count smallint NOT NULL CHECK (auto_link_count BETWEEN 0 AND 32),
  manual_review_count smallint NOT NULL CHECK (manual_review_count BETWEEN 0 AND 32),
  deferred_resolution_count smallint NOT NULL CHECK (deferred_resolution_count BETWEEN 0 AND 32),
  rejected_count smallint NOT NULL CHECK (rejected_count BETWEEN 0 AND 32),
  blocking_code_count smallint NOT NULL CHECK (blocking_code_count BETWEEN 0 AND 32),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
  FOREIGN KEY(owner_user_id,packet_id) REFERENCES memory.evidence_extraction_packet_v5(owner_user_id,packet_id),
  FOREIGN KEY(owner_user_id,job_id) REFERENCES memory.evidence_extraction_job(owner_user_id,job_id),
  FOREIGN KEY(owner_user_id,evidence_id) REFERENCES memory.evidence(owner_user_id,evidence_id),
  CHECK (entity_mention_count+observation_count+comparison_hint_count>=1),
  CHECK (auto_link_count+manual_review_count+deferred_resolution_count+rejected_count=entity_mention_count)
);
CREATE INDEX v5_2_openai_packet_route_event_owner_time_idx ON memory.v5_2_openai_packet_route_event(owner_user_id,created_at,route_event_id);
ALTER TABLE memory.v5_2_openai_packet_route_event OWNER TO sage;
ALTER TABLE memory.v5_2_openai_packet_route_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_openai_packet_route_event FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.v5_2_openai_packet_route_event USING (owner_user_id=memory.current_actor_user_id()) WITH CHECK (owner_user_id=memory.current_actor_user_id());
GRANT SELECT,INSERT ON memory.v5_2_openai_packet_route_event TO memory_v5_2_local_router_maintainer;
GRANT SELECT ON memory.evidence_extraction_packet_v5 TO memory_v5_2_local_router_maintainer;
