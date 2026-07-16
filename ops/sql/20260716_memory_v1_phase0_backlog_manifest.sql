BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH active_owner(owner_user_id) AS (
  VALUES
    ('1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid),
    ('557ea042-cb82-48f8-9429-472e96c957ef'::uuid),
    ('d839b4bc-0bd2-4f2d-aafe-0f3f75883db8'::uuid),
    ('818b60b9-89bd-442a-998c-fc1924184dfc'::uuid),
    ('5c9f624a-a66d-4183-babb-b3a0f0f4e733'::uuid),
    ('673d64a3-c4ba-4d1c-89e3-e0c579022fad'::uuid)
), classified AS (
  SELECT
    job.job_id,
    job.owner_user_id,
    job.pipeline_version,
    job.source_system,
    job.source_external_id,
    job.source_sha256,
    job.source_recorded_at,
    job.status,
    job.priority,
    job.attempts,
    job.created_at,
    log.id IS NOT NULL AS source_present,
    coalesce(log.owner_user_id=job.owner_user_id,false) AS source_owner_match,
    coalesce(log.source='frontend/chat:user',false) AS source_contract_match,
    coalesce(
      encode(digest(coalesce(log.text,''),'sha256'),'hex')=job.source_sha256,
      false
    ) AS source_hash_match,
    active_owner.owner_user_id IS NOT NULL AS active_owner,
    coalesce(evidence.matching_evidence,false) AS matching_evidence,
    coalesce(evidence.evidence_ids,ARRAY[]::text[]) AS evidence_ids
  FROM memory.consolidation_job AS job
  LEFT JOIN public.chat_log AS log
    ON log.id::text=job.source_external_id
  LEFT JOIN active_owner
    ON active_owner.owner_user_id=job.owner_user_id
  LEFT JOIN LATERAL (
    SELECT
      bool_or(item.content_sha256=job.source_sha256) AS matching_evidence,
      array_agg(item.evidence_id::text ORDER BY item.evidence_id::text)
        FILTER (WHERE item.content_sha256=job.source_sha256) AS evidence_ids
    FROM memory.evidence AS item
    WHERE item.owner_user_id=job.owner_user_id
      AND item.source_system=job.source_system
      AND item.external_id IN (
        job.source_external_id,
        'chat_log:' || job.source_external_id
      )
  ) AS evidence ON true
  WHERE job.status='pending'
), rows AS (
  SELECT
    job_id,
    jsonb_build_object(
      'job_id',job_id,
      'owner_user_id',owner_user_id,
      'pipeline_version',pipeline_version,
      'source_system',source_system,
      'source_external_id',source_external_id,
      'source_sha256',source_sha256,
      'source_recorded_at',to_char(
        source_recorded_at AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
      ),
      'status',status,
      'priority',priority,
      'attempts',attempts,
      'created_at',to_char(
        created_at AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
      ),
      'source_present',source_present,
      'source_owner_match',source_owner_match,
      'source_contract_match',source_contract_match,
      'source_hash_match',source_hash_match,
      'active_owner',active_owner,
      'matching_evidence',matching_evidence,
      'evidence_ids',to_jsonb(evidence_ids),
      'classification',CASE
        WHEN NOT source_present THEN 'source_missing'
        WHEN NOT source_owner_match THEN 'source_owner_mismatch'
        WHEN NOT source_contract_match THEN 'source_contract_mismatch'
        WHEN NOT source_hash_match THEN 'source_hash_changed'
        WHEN matching_evidence THEN 'already_represented_by_evidence'
        WHEN NOT active_owner THEN 'inactive_or_deleted_owner_review'
        ELSE 'active_owner_v5_eligible'
      END
    ) AS row_json
  FROM classified
)
SELECT coalesce(jsonb_agg(row_json ORDER BY job_id), '[]'::jsonb)::text
FROM rows;

COMMIT;
