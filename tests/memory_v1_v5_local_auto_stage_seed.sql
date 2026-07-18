\set ON_ERROR_STOP on

BEGIN;
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
  evidence_content_sha256,decision_fingerprint,actor_user_id,invoked_by_role,
  details
) VALUES (
  :'terminal_id'::uuid,:'owner_user_id'::uuid,:'evidence_id'::uuid,
  'memory_v1_local_auto_stage_fixture_v1','dispatched','eligible_dispatched',
  :'evidence_content_sha256',repeat('1',64),:'owner_user_id'::uuid,'sage',
  '{"synthetic_clone_fixture":true}'::jsonb
);

INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
  evidence_content_sha256,route,intake_reason_code,status,attempts,result
) VALUES (
  :'job_id'::uuid,:'owner_user_id'::uuid,:'evidence_id'::uuid,
  :'terminal_id'::uuid,'memory_v1_local_auto_stage_fixture_v1',
  :'evidence_content_sha256','relational_extraction','eligible_unprocessed',
  'review_required',1,'{"synthetic_clone_fixture":true}'::jsonb
);

INSERT INTO memory.evidence_extraction_packet_v5_local(
  packet_id,owner_user_id,operation_id,job_id,evidence_id,
  evidence_content_sha256,provider_id,provider_version,
  provider_model_sha256,model_file_sha256,runtime_revision_sha256,
  policy_compiler_sha256,provider_output_sha256,validator_packet_sha256,
  packet_storage_sha256,normalized_packet,manual_review_required,
  local_model_calls,external_model_calls,entity_mention_count,
  observation_count,comparison_hint_count,deferral_count
) VALUES (
  :'packet_id'::uuid,:'owner_user_id'::uuid,
  'a3800000-0000-4000-8000-000000000001',:'job_id'::uuid,
  :'evidence_id'::uuid,:'evidence_content_sha256','local_llama_cpp',
  'synthetic_clone_fixture_v1',repeat('2',64),repeat('3',64),repeat('4',64),
  repeat('5',64),repeat('6',64),:'validator_packet_sha256',
  encode(public.digest(convert_to(:'normalized_packet'::jsonb::text,'UTF8'),
    'sha256'),'hex'),
  :'normalized_packet'::jsonb,true,1,0,1,1,0,0
);

INSERT INTO memory.v5_local_packet_review_artifact(
  artifact_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
  review_id,request_id,review_report_sha256,stage_bundle_sha256,
  packet_storage_sha256,repository_commit,auto_link_count,
  manual_review_count,deferred_count,rejected_count,blocking_code_count,
  review_disposition
) SELECT
  :'artifact_id'::uuid,:'owner_user_id'::uuid,:'artifact_operation_id'::uuid,
  :'packet_id'::uuid,:'job_id'::uuid,:'evidence_id'::uuid,:'review_id'::uuid,
  :'request_id'::uuid,:'review_report_sha256',:'stage_bundle_sha256',
  packet_storage_sha256,:'repository_commit',1,0,0,0,0,
  'manual_review_required'
FROM memory.evidence_extraction_packet_v5_local
WHERE owner_user_id=:'owner_user_id'::uuid AND packet_id=:'packet_id'::uuid;
COMMIT;
