BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local review refresh reextract install requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.entity') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_local_packet_review_artifact') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL THEN
    RAISE EXCEPTION 'local review refresh reextract prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_local_review_refresh_reextract_v1(
  p_prior_packet_id uuid,
  p_expected_review_report_sha256 text,
  p_expected_stage_bundle_sha256 text
)
RETURNS TABLE(
  prior_packet_id uuid,
  prior_job_id uuid,
  artifact_id uuid,
  evidence_id uuid,
  evidence_content_sha256 text,
  prior_packet_storage_sha256 text,
  prior_policy_compiler_sha256 text,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  active_self_entity_count integer,
  reason_code text,
  next_selector_version text,
  next_policy_compiler_version text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  compiler_v6 text := encode(public.digest(convert_to(
    to_jsonb('memory_v1_local_policy_compiler_v6'::text)::text,'UTF8'
  ),'sha256'),'hex');
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local review refresh plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_prior_packet_id IS NULL
     OR p_expected_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_stage_bundle_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'local review refresh plan inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    artifact.artifact_id,
    packet.evidence_id,
    packet.evidence_content_sha256,
    packet.packet_storage_sha256,
    packet.policy_compiler_sha256,
    artifact.review_report_sha256,
    artifact.stage_bundle_sha256,
    (
      SELECT count(*)::integer
      FROM memory.entity AS self_entity
      WHERE self_entity.owner_user_id=actor
        AND self_entity.entity_type='self'
        AND self_entity.status='active'
        AND self_entity.metadata->>'identity_state'='trusted_owner_self'
    ),
    'trusted_owner_self_became_available'::text,
    '20260719_v5_self_bootstrap_review_refresh_v1'::text,
    'memory_v1_local_policy_compiler_v6'::text
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  JOIN memory.v5_local_packet_review_artifact AS artifact
    ON artifact.owner_user_id=packet.owner_user_id
   AND artifact.packet_id=packet.packet_id
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_prior_packet_id
    AND packet.provider_id='local_llama_cpp'
    AND packet.policy_compiler_sha256=compiler_v6
    AND packet.local_model_calls=1
    AND packet.external_model_calls=0
    AND packet.manual_review_required
    AND packet.entity_mention_count=2
    AND packet.observation_count>0
    AND artifact.review_report_sha256=p_expected_review_report_sha256
    AND artifact.stage_bundle_sha256=p_expected_stage_bundle_sha256
    AND artifact.auto_link_count=0
    AND artifact.manual_review_count=1
    AND artifact.deferred_count=1
    AND artifact.rejected_count=0
    AND artifact.blocking_code_count=3
    AND artifact.review_disposition='manual_review_required'
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND evidence.content_sha256=packet.evidence_content_sha256
    AND packet.packet_storage_sha256=encode(public.digest(convert_to(
      packet.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND (
      SELECT count(*) FROM memory.entity AS self_entity
      WHERE self_entity.owner_user_id=actor
        AND self_entity.entity_type='self'
        AND self_entity.status='active'
        AND self_entity.metadata->>'identity_state'='trusted_owner_self'
    )=1
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=packet.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.evidence_extraction_job AS next_job
      WHERE next_job.owner_user_id=actor
        AND next_job.evidence_id=packet.evidence_id
        AND next_job.selector_version=
          '20260719_v5_self_bootstrap_review_refresh_v1'
    );
END
$function$;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
  p_operation_id uuid,
  p_new_job_id uuid,
  p_new_terminal_id uuid,
  p_prior_packet_id uuid,
  p_expected_content_sha256 text,
  p_expected_packet_storage_sha256 text,
  p_expected_review_report_sha256 text,
  p_expected_stage_bundle_sha256 text,
  p_selector_version text,
  p_next_policy_compiler_version text
)
RETURNS TABLE(
  job_id uuid,
  intake_terminal_id uuid,
  status text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replay_event memory.evidence_extraction_event%ROWTYPE;
  replay_job memory.evidence_extraction_job%ROWTYPE;
  replay_terminal memory.evidence_intake_terminal%ROWTYPE;
  fingerprint text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local review refresh apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_new_job_id IS NULL
     OR p_new_terminal_id IS NULL OR p_prior_packet_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_selector_version<>
       '20260719_v5_self_bootstrap_review_refresh_v1'
     OR p_next_policy_compiler_version<>
       'memory_v1_local_policy_compiler_v6' THEN
    RAISE EXCEPTION 'local review refresh inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_local_review_refresh_reextract',actor::text,
    p_prior_packet_id::text,p_selector_version),0));

  SELECT event.* INTO replay_event
  FROM memory.evidence_extraction_event AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    SELECT value.* INTO replay_job
    FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id=actor AND value.job_id=p_new_job_id;
    SELECT value.* INTO replay_terminal
    FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id=actor
      AND value.terminal_id=p_new_terminal_id;
    IF replay_job.job_id IS NULL OR replay_terminal.terminal_id IS NULL
       OR replay_event.job_id<>p_new_job_id
       OR replay_event.event_type<>'queued'
       OR replay_event.from_status IS NOT NULL
       OR replay_event.to_status<>'pending'
       OR replay_event.actor_type<>'admin'
       OR replay_event.actor_ref IS DISTINCT FROM
         'local_review_refresh_reextract_v1'
       OR replay_event.details->>'prior_packet_id'
          IS DISTINCT FROM p_prior_packet_id::text
       OR replay_event.details->>'expected_review_report_sha256'
          IS DISTINCT FROM p_expected_review_report_sha256
       OR replay_event.details->>'expected_stage_bundle_sha256'
          IS DISTINCT FROM p_expected_stage_bundle_sha256
       OR replay_job.intake_terminal_id<>p_new_terminal_id
       OR replay_job.evidence_id<>replay_terminal.evidence_id
       OR replay_job.selector_version<>p_selector_version
       OR replay_job.evidence_content_sha256<>p_expected_content_sha256
       OR replay_job.route<>'relational_extraction'
       OR replay_terminal.selector_version<>p_selector_version
       OR replay_terminal.evidence_content_sha256<>
          p_expected_content_sha256
       OR replay_terminal.outcome<>'dispatched'
       OR replay_terminal.reason_code<>'eligible_dispatched' THEN
      RAISE EXCEPTION 'local review refresh replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replay_job.job_id,replay_terminal.terminal_id,
      replay_job.status::text,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_local_review_refresh_reextract_v1(
    p_prior_packet_id,p_expected_review_report_sha256,
    p_expected_stage_bundle_sha256
  );
  IF NOT FOUND
     OR planned.evidence_content_sha256<>p_expected_content_sha256
     OR planned.prior_packet_storage_sha256<>
        p_expected_packet_storage_sha256
     OR planned.review_report_sha256<>
        p_expected_review_report_sha256
     OR planned.stage_bundle_sha256<>
        p_expected_stage_bundle_sha256
     OR planned.active_self_entity_count<>1
     OR planned.reason_code<>'trusted_owner_self_became_available'
     OR planned.next_selector_version<>p_selector_version
     OR planned.next_policy_compiler_version<>
        p_next_policy_compiler_version THEN
    RAISE EXCEPTION 'local review refresh plan changed'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id=actor
      AND (value.job_id=p_new_job_id
        OR value.selector_version=p_selector_version
           AND value.evidence_id=planned.evidence_id)
  ) OR EXISTS (
    SELECT 1 FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id=actor
      AND value.terminal_id=p_new_terminal_id
  ) THEN
    RAISE EXCEPTION 'local review refresh identity already exists'
      USING ERRCODE='23505';
  END IF;

  fingerprint := encode(public.digest(convert_to(concat_ws('|',
    'memory_v1_v5_local_review_refresh_reextract_v1',actor::text,
    planned.evidence_id::text,p_prior_packet_id::text,
    p_expected_content_sha256,p_expected_packet_storage_sha256,
    p_expected_review_report_sha256,p_expected_stage_bundle_sha256,
    p_selector_version,p_next_policy_compiler_version
  ),'UTF8'),'sha256'),'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,outcome,
    reason_code,evidence_content_sha256,decision_fingerprint,
    actor_user_id,invoked_by_role,details
  ) VALUES (
    p_new_terminal_id,actor,planned.evidence_id,p_selector_version,
    'dispatched','eligible_dispatched',p_expected_content_sha256,
    fingerprint,actor,session_user,jsonb_build_object(
      'route','relational_extraction',
      'extraction_job_id',p_new_job_id,
      'reextract_reason','trusted_owner_self_became_available',
      'prior_packet_id',p_prior_packet_id,
      'review_artifact_id',planned.artifact_id,
      'expected_review_report_sha256',p_expected_review_report_sha256,
      'expected_stage_bundle_sha256',p_expected_stage_bundle_sha256,
      'next_policy_compiler_version',p_next_policy_compiler_version,
      'source_prose_copied',false
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
    evidence_content_sha256,route,intake_reason_code,status
  ) VALUES (
    p_new_job_id,actor,planned.evidence_id,p_new_terminal_id,
    p_selector_version,p_expected_content_sha256,
    'relational_extraction','eligible_unprocessed','pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,from_status,to_status,
    actor_type,actor_ref,details
  ) VALUES (
    actor,p_new_job_id,p_operation_id,'queued',NULL,'pending',
    'admin','local_review_refresh_reextract_v1',jsonb_build_object(
      'prior_packet_id',p_prior_packet_id,
      'expected_content_sha256',p_expected_content_sha256,
      'expected_packet_storage_sha256',p_expected_packet_storage_sha256,
      'expected_review_report_sha256',p_expected_review_report_sha256,
      'expected_stage_bundle_sha256',p_expected_stage_bundle_sha256,
      'reason_code','trusted_owner_self_became_available',
      'next_policy_compiler_version',p_next_policy_compiler_version,
      'source_prose_copied',false
    )
  );

  RETURN QUERY SELECT p_new_job_id,p_new_terminal_id,'pending'::text,
    'applied'::text;
END
$function$;

GRANT SELECT ON memory.entity,memory.v5_local_packet_review_artifact
  TO memory_v5_local_reextract_maintainer;

ALTER FUNCTION memory.plan_owner_v5_local_review_refresh_reextract_v1(
  uuid,text,text
) OWNER TO memory_v5_local_reextract_maintainer;
ALTER FUNCTION memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_review_refresh_reextract_v1(uuid,text,text)
  FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text
  ) FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_review_refresh_reextract_v1(uuid,text,text)
  TO brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
    uuid,uuid,uuid,uuid,text,text,text,text,text,text
  ) TO brains_app;

COMMIT;
