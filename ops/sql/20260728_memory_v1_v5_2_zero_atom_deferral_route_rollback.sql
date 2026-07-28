BEGIN;

DROP FUNCTION IF EXISTS
  memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
    uuid,uuid,uuid,text,text,text,text[]
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer);

ALTER TABLE memory.v5_2_local_packet_route_event
  DROP CONSTRAINT v5_2_local_packet_route_event_check,
  DROP CONSTRAINT v5_2_local_packet_route_event_reason_code_check;

ALTER TABLE memory.v5_2_local_packet_route_event
  ADD CONSTRAINT v5_2_local_packet_route_event_reason_code_check
  CHECK (reason_code = ANY (ARRAY[
    'deferral_only_no_stage_v5_2'::text,
    'reviewable_relational_packet_v5_2'::text
  ])),
  ADD CONSTRAINT v5_2_local_packet_route_event_check
  CHECK (
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_no_stage_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 4
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence'
      ]::text[]
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    )
    OR
    (
      route='manual_review_artifact_ready'
      AND reason_code='reviewable_relational_packet_v5_2'
      AND entity_mention_count+observation_count+comparison_hint_count>=1
      AND cardinality(source_deferral_reason_codes)=0
      AND review_id IS NOT NULL
      AND request_id IS NOT NULL
      AND review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND review_report_sha256 ~ '^[0-9a-f]{64}$'
      AND stage_bundle_sha256 ~ '^[0-9a-f]{64}$'
      AND repository_commit ~ '^[0-9a-f]{40}$'
      AND auto_link_count BETWEEN 0 AND 32
      AND manual_review_count BETWEEN 0 AND 32
      AND deferred_resolution_count BETWEEN 0 AND 32
      AND rejected_count BETWEEN 0 AND 32
      AND blocking_code_count BETWEEN 0 AND 32
      AND auto_link_count+manual_review_count
            +deferred_resolution_count+rejected_count=entity_mention_count
    )
  );

COMMIT;
