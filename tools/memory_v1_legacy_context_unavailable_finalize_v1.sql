\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

SELECT set_config('app.user_id', :'owner_user_id', true);

CREATE TEMP TABLE expected_counts (
  total integer NOT NULL,
  deferred integer NOT NULL,
  superseded integer NOT NULL,
  deleted integer NOT NULL
) ON COMMIT DROP;

INSERT INTO expected_counts(total, deferred, superseded, deleted)
VALUES (
  :'expected_total'::integer,
  :'expected_deferred'::integer,
  :'expected_superseded'::integer,
  :'expected_deleted'::integer
);

CREATE TEMP TABLE raw_contextless_jobs ON COMMIT DROP AS
SELECT
  job.owner_user_id,
  job.job_id,
  job.evidence_id,
  job.status::text AS from_status,
  job.selector_version,
  job.evidence_content_sha256,
  evidence.status::text AS evidence_status
FROM memory.evidence_extraction_job AS job
JOIN memory.evidence AS evidence
  ON evidence.owner_user_id = job.owner_user_id
 AND evidence.evidence_id = job.evidence_id
WHERE job.owner_user_id = :'owner_user_id'::uuid
  AND job.route = 'relational_extraction'
  AND job.selector_version = '20260717_v2'
  AND job.status = 'pending'
  AND job.attempts = 0
  AND job.available_at <= clock_timestamp()
  AND job.lease_token IS NULL
  AND job.lease_expires_at IS NULL
  AND evidence.source_system = 'public.chat_log'
  AND evidence.content_sha256 = job.evidence_content_sha256
  AND NOT (evidence.metadata ? 'source_id');

CREATE TEMP TABLE classified_contextless_jobs ON COMMIT DROP AS
WITH classified AS (
  SELECT
    raw.*,
    EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS newer_job
      JOIN memory.evidence_extraction_packet_v5_local AS packet
        ON packet.owner_user_id = newer_job.owner_user_id
       AND packet.job_id = newer_job.job_id
      WHERE newer_job.owner_user_id = raw.owner_user_id
        AND newer_job.evidence_id = raw.evidence_id
        AND newer_job.job_id <> raw.job_id
        AND newer_job.status IN (
          'review_required', 'completed', 'skipped'
        )
    ) AS has_newer_processing,
    EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id = raw.owner_user_id
        AND packet.evidence_id = raw.evidence_id
    ) OR EXISTS (
      SELECT 1
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id = raw.owner_user_id
        AND stage.evidence_id = raw.evidence_id
    ) OR EXISTS (
      SELECT 1
      FROM memory.observation AS observation
      WHERE observation.owner_user_id = raw.owner_user_id
        AND observation.evidence_id = raw.evidence_id
    ) OR EXISTS (
      SELECT 1
      FROM memory.claim_evidence AS claim_link
      WHERE claim_link.owner_user_id = raw.owner_user_id
        AND claim_link.evidence_id = raw.evidence_id
    ) AS has_downstream_processing
  FROM raw_contextless_jobs AS raw
),
admitted AS (
  SELECT
    classified.*,
    CASE
      WHEN evidence_status = 'deleted'
        THEN 'skipped'
      WHEN has_newer_processing
        THEN 'skipped'
      WHEN evidence_status = 'active'
       AND NOT has_downstream_processing
        THEN 'deferred'
      ELSE NULL
    END AS disposition,
    CASE
      WHEN evidence_status = 'deleted'
        THEN 'source_evidence_deleted'
      WHEN has_newer_processing
        THEN 'superseded_by_existing_processing'
      WHEN evidence_status = 'active'
       AND NOT has_downstream_processing
        THEN 'context_unavailable'
      ELSE NULL
    END AS reason_code
  FROM classified
),
with_basis AS (
  SELECT
    admitted.*,
    encode(
      public.digest(
        convert_to(
          jsonb_build_object(
            'contract_version',
              'memory_v1_legacy_context_unavailable_finalize_v1',
            'owner_user_id', admitted.owner_user_id,
            'job_id', admitted.job_id,
            'evidence_id', admitted.evidence_id,
            'selector_version', admitted.selector_version,
            'evidence_content_sha256',
              admitted.evidence_content_sha256,
            'disposition', admitted.disposition,
            'reason_code', admitted.reason_code
          )::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ) AS basis_sha256
  FROM admitted
  WHERE disposition IS NOT NULL
),
with_operation AS (
  SELECT
    with_basis.*,
    (
      substr(basis_sha256, 1, 8) || '-' ||
      substr(basis_sha256, 9, 4) || '-' ||
      substr(basis_sha256, 13, 4) || '-' ||
      substr(basis_sha256, 17, 4) || '-' ||
      substr(basis_sha256, 21, 12)
    )::uuid AS operation_id
  FROM with_basis
)
SELECT
  with_operation.*,
  encode(
    public.digest(
      convert_to(
        jsonb_build_object(
          'basis_sha256', with_operation.basis_sha256,
          'operation_id', with_operation.operation_id,
          'status', 'skipped',
          'disposition', with_operation.disposition,
          'reason_code', with_operation.reason_code,
          'local_model_calls', 0,
          'external_model_calls', 0
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  ) AS record_sha256
FROM with_operation;

DO $preflight$
DECLARE
  expected expected_counts%ROWTYPE;
  raw_count integer;
  classified_count integer;
  deferred_count integer;
  superseded_count integer;
  deleted_count integer;
BEGIN
  SELECT * INTO expected FROM expected_counts;
  SELECT count(*) INTO raw_count FROM raw_contextless_jobs;
  SELECT
    count(*),
    count(*) FILTER (WHERE reason_code = 'context_unavailable'),
    count(*) FILTER (
      WHERE reason_code = 'superseded_by_existing_processing'
    ),
    count(*) FILTER (
      WHERE reason_code = 'source_evidence_deleted'
    )
  INTO classified_count, deferred_count, superseded_count, deleted_count
  FROM classified_contextless_jobs;

  IF raw_count <> expected.total
     OR classified_count <> expected.total
     OR deferred_count <> expected.deferred
     OR superseded_count <> expected.superseded
     OR deleted_count <> expected.deleted THEN
    RAISE EXCEPTION
      'legacy contextless count drift: raw %, classified %, deferred %, superseded %, deleted %',
      raw_count, classified_count, deferred_count, superseded_count,
      deleted_count
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM classified_contextless_jobs
    WHERE owner_user_id <> memory.current_actor_user_id()
       OR basis_sha256 !~ '^[0-9a-f]{64}$'
       OR record_sha256 !~ '^[0-9a-f]{64}$'
  ) THEN
    RAISE EXCEPTION 'legacy contextless owner or hash drift'
      USING ERRCODE = '23514';
  END IF;
END
$preflight$;

INSERT INTO memory.evidence_extraction_event(
  owner_user_id,
  job_id,
  operation_id,
  event_type,
  from_status,
  to_status,
  actor_type,
  actor_ref,
  details
)
SELECT
  target.owner_user_id,
  target.job_id,
  target.operation_id,
  'skipped',
  target.from_status::memory.evidence_extraction_job_status,
  'skipped',
  'system',
  'memory_v1_legacy_context_unavailable_finalize_v1',
  jsonb_build_object(
    'contract_version',
      'memory_v1_legacy_context_unavailable_finalize_v1',
    'disposition', target.disposition,
    'reason_code', target.reason_code,
    'basis_sha256', target.basis_sha256,
    'record_sha256', target.record_sha256,
    'local_model_calls', 0,
    'external_model_calls', 0,
    'claims', 0,
    'qdrant', 0,
    'prompt_influence', 0
  )
FROM classified_contextless_jobs AS target
WHERE NOT EXISTS (
  SELECT 1
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id = target.owner_user_id
    AND event.job_id = target.job_id
    AND event.operation_id = target.operation_id
);

UPDATE memory.evidence_extraction_job AS job
SET
  status = 'skipped',
  worker_id = 'memory_v1_legacy_context_unavailable_finalize_v1',
  last_error = NULL,
  result = jsonb_set(
    COALESCE(job.result, '{}'::jsonb),
    '{final}',
    jsonb_build_object(
      'status', 'skipped',
      'sha256', target.record_sha256,
      'payload', jsonb_build_object(
        'contract_version',
          'memory_v1_legacy_context_unavailable_finalize_v1',
        'disposition', target.disposition,
        'reason_code', target.reason_code,
        'basis_sha256', target.basis_sha256,
        'local_model_calls', 0,
        'external_model_calls', 0,
        'claims', 0,
        'qdrant', 0,
        'prompt_influence', 0
      )
    ),
    true
  )
FROM classified_contextless_jobs AS target
WHERE job.owner_user_id = target.owner_user_id
  AND job.job_id = target.job_id
  AND job.status = 'pending'
  AND job.attempts = 0
  AND job.lease_token IS NULL
  AND job.lease_expires_at IS NULL;

SELECT jsonb_build_object(
  'contract_version',
    'memory_v1_legacy_context_unavailable_finalize_v1',
  'owner_user_id_sha256',
    encode(
      public.digest(
        convert_to(:'owner_user_id', 'UTF8'),
        'sha256'
      ),
      'hex'
    ),
  'candidate_count',
    (SELECT count(*) FROM classified_contextless_jobs),
  'disposition_counts',
    COALESCE(
      (
        SELECT jsonb_object_agg(disposition, count)
        FROM (
          SELECT disposition, count(*) AS count
          FROM classified_contextless_jobs
          GROUP BY disposition
          ORDER BY disposition
        ) AS counts
      ),
      '{}'::jsonb
    ),
  'reason_counts',
    COALESCE(
      (
        SELECT jsonb_object_agg(reason_code, count)
        FROM (
          SELECT reason_code, count(*) AS count
          FROM classified_contextless_jobs
          GROUP BY reason_code
          ORDER BY reason_code
        ) AS counts
      ),
      '{}'::jsonb
    ),
  'local_model_calls', 0,
  'external_model_calls', 0,
  'claims', 0,
  'qdrant', 0,
  'prompt_influence', 0
) AS report;

COMMIT;
