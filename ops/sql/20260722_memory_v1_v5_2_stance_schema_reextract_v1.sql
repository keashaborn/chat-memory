BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract install requires sage';
  END IF;
  IF to_regrole('brains_app') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_local_inference_event') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract prerequisites are absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_local_reextract_maintainer'
  ) THEN
    CREATE ROLE memory_v5_local_reextract_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$preflight$;

DO $persistence_compatibility$
DECLARE
  persistence_oid constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_before constant text :=
    '0ca8e0240ce8d4d9c1d43f3457c4e2b2a6475176c47a240059f644125af41f49';
  expected_after constant text :=
    '78ee8c17d613e9db9d3c040c2e73ed28f50f0b9898cecca7ea5d892c91a95830';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v4','UTF8'
       ),'sha256'),'hex')
     )$new$;
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(persistence_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha=expected_after THEN
    IF (length(source)-length(replace(source,new_fragment,'')))
         / length(new_fragment)<>1 THEN
      RAISE EXCEPTION 'V5.2 persistence v4 installed body changed'
        USING ERRCODE='23514';
    END IF;
    RETURN;
  END IF;
  IF source_sha<>expected_before
     OR (length(source)-length(replace(source,old_fragment,'')))
          / length(old_fragment)<>1 THEN
    RAISE EXCEPTION 'V5.2 persistence v4 baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_fragment,new_fragment);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_after THEN
    RAISE EXCEPTION 'V5.2 persistence v4 replacement changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$persistence_compatibility$;

ALTER FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_stance_schema_reextract_v1(
  p_prior_packet_id uuid
)
RETURNS TABLE(
  prior_packet_id uuid,
  prior_job_id uuid,
  evidence_id uuid,
  evidence_content_sha256 text,
  prior_packet_storage_sha256 text,
  prior_policy_compiler_sha256 text,
  prior_selector_version text,
  prior_disposition_count integer,
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
  prior_compiler_sha text := encode(public.digest(convert_to(
    'memory_v1_semantic_policy_compiler_v3','UTF8'
  ),'sha256'),'hex');
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_prior_packet_id IS NULL THEN
    RAISE EXCEPTION 'prior packet id is required' USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
    packet.evidence_content_sha256,
    packet.packet_storage_sha256,
    packet.policy_compiler_sha256,
    job.selector_version,
    (
      SELECT count(*)::integer
      FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=actor
        AND disposition.packet_id=packet.packet_id
        AND disposition.disposition='terminal_no_stage'
    ),
    'semantic_stance_object_schema_mismatch'::text,
    '20260722_v5_2_stance_schema_reextract_v1'::text,
    'memory_v1_semantic_policy_compiler_v4'::text
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_prior_packet_id
    AND packet.provider_id='local_llama_cpp'
    AND packet.policy_compiler_sha256=prior_compiler_sha
    AND packet.local_model_calls=1
    AND packet.external_model_calls=0
    AND NOT packet.manual_review_required
    AND packet.entity_mention_count=0
    AND packet.observation_count=0
    AND packet.comparison_hint_count=0
    AND packet.deferral_count=1
    AND jsonb_typeof(packet.normalized_packet)='object'
    AND jsonb_array_length(packet.normalized_packet->'entity_mentions')=0
    AND jsonb_array_length(packet.normalized_packet->'observations')=0
    AND jsonb_array_length(packet.normalized_packet->'comparison_hints')=0
    AND jsonb_array_length(packet.normalized_packet->'deferrals')=1
    AND packet.normalized_packet#>>'{deferrals,0,reason_code}'=
        'insufficient_evidence'
    AND packet.normalized_packet#>>'{deferrals,0,memory_shape}'='none'
    AND EXISTS (
      SELECT 1
      FROM memory.v5_local_inference_event AS completion
      WHERE completion.owner_user_id=actor
        AND completion.job_id=packet.job_id
        AND completion.action='completed'
        AND completion.outcome='accepted'
        AND completion.policy_compiler_sha256=prior_compiler_sha
        AND completion.packet_storage_sha256=packet.packet_storage_sha256
        AND completion.local_model_calls=1
        AND completion.external_model_calls=0
    )
    AND packet.packet_storage_sha256=encode(public.digest(convert_to(
      packet.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND evidence.content_sha256=packet.evidence_content_sha256
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
          '20260722_v5_2_stance_schema_reextract_v1'
    );
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_owner_v5_2_stance_schema_reextract_v1(
  p_operation_id uuid,
  p_new_job_id uuid,
  p_new_terminal_id uuid,
  p_prior_packet_id uuid,
  p_expected_content_sha256 text,
  p_expected_packet_storage_sha256 text,
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
    RAISE EXCEPTION 'local V5.2 stance schema reextract apply requires brains_app session'
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
     OR p_selector_version<>'20260722_v5_2_stance_schema_reextract_v1'
     OR p_next_policy_compiler_version<>
       'memory_v1_semantic_policy_compiler_v4' THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_stance_schema_reextract_v1',actor::text,
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
    WHERE value.owner_user_id=actor AND value.terminal_id=p_new_terminal_id;
    IF replay_job.job_id IS NULL OR replay_terminal.terminal_id IS NULL
       OR replay_event.job_id<>p_new_job_id
       OR replay_event.event_type<>'queued'
       OR replay_event.from_status IS NOT NULL
       OR replay_event.to_status<>'pending'
       OR replay_event.actor_type<>'admin'
       OR replay_event.actor_ref IS DISTINCT FROM
         'local_v5_2_stance_schema_reextract_v1'
       OR replay_event.details->>'prior_packet_id'
          IS DISTINCT FROM p_prior_packet_id::text
       OR replay_event.details->>'expected_content_sha256'
          IS DISTINCT FROM p_expected_content_sha256
       OR replay_event.details->>'expected_packet_storage_sha256'
          IS DISTINCT FROM p_expected_packet_storage_sha256
       OR replay_event.details->>'next_policy_compiler_version'
          IS DISTINCT FROM p_next_policy_compiler_version
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
      RAISE EXCEPTION 'local V5.2 stance schema reextract replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replay_job.job_id,replay_terminal.terminal_id,
      replay_job.status::text,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_stance_schema_reextract_v1(p_prior_packet_id);
  IF NOT FOUND
     OR planned.evidence_content_sha256<>p_expected_content_sha256
     OR planned.prior_packet_storage_sha256<>
        p_expected_packet_storage_sha256
     OR planned.reason_code<>'semantic_stance_object_schema_mismatch'
     OR planned.next_selector_version<>p_selector_version
     OR planned.next_policy_compiler_version<>
        p_next_policy_compiler_version THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract plan changed'
      USING ERRCODE='23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.evidence_extraction_job AS value
    WHERE value.owner_user_id=actor
      AND (value.job_id=p_new_job_id
        OR (value.evidence_id=planned.evidence_id
          AND value.selector_version=p_selector_version))
  ) OR EXISTS (
    SELECT 1 FROM memory.evidence_intake_terminal AS value
    WHERE value.owner_user_id=actor
      AND (value.terminal_id=p_new_terminal_id
        OR (value.evidence_id=planned.evidence_id
          AND value.selector_version=p_selector_version))
  ) THEN
    RAISE EXCEPTION 'local V5.2 stance schema reextract target identifiers conflict'
      USING ERRCODE='23514';
  END IF;

  fingerprint := encode(public.digest(convert_to(jsonb_build_object(
    'owner_user_id',actor,
    'evidence_id',planned.evidence_id,
    'selector_version',p_selector_version,
    'evidence_content_sha256',p_expected_content_sha256,
    'prior_packet_id',p_prior_packet_id,
    'prior_packet_storage_sha256',p_expected_packet_storage_sha256,
    'reason_code','semantic_stance_object_schema_mismatch',
    'next_policy_compiler_version',p_next_policy_compiler_version
  )::text,'UTF8'),'sha256'),'hex');

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
      'reextract_reason','semantic_stance_object_schema_mismatch',
      'prior_packet_id',p_prior_packet_id,
      'prior_packet_storage_sha256',p_expected_packet_storage_sha256,
      'prior_policy_compiler_sha256',planned.prior_policy_compiler_sha256,
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
    'admin','local_v5_2_stance_schema_reextract_v1',jsonb_build_object(
      'prior_packet_id',p_prior_packet_id,
      'expected_content_sha256',p_expected_content_sha256,
      'expected_packet_storage_sha256',p_expected_packet_storage_sha256,
      'reason_code','semantic_stance_object_schema_mismatch',
      'next_policy_compiler_version',p_next_policy_compiler_version,
      'source_prose_copied',false
    )
  );

  RETURN QUERY SELECT p_new_job_id,p_new_terminal_id,'pending'::text,
    'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_local_reextract_maintainer;
GRANT SELECT ON memory.evidence,memory.evidence_intake_terminal,
  memory.evidence_extraction_job,memory.evidence_extraction_event,
  memory.evidence_extraction_packet_v5_local,
  memory.v5_local_inference_event,memory.v5_local_packet_disposition,
  memory.relational_stage_batch
TO memory_v5_local_reextract_maintainer;
GRANT INSERT ON memory.evidence_intake_terminal,
  memory.evidence_extraction_job,memory.evidence_extraction_event
TO memory_v5_local_reextract_maintainer;

ALTER FUNCTION memory.plan_owner_v5_2_stance_schema_reextract_v1(uuid)
  OWNER TO memory_v5_local_reextract_maintainer;
ALTER FUNCTION memory.enqueue_owner_v5_2_stance_schema_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_stance_schema_reextract_v1(uuid)
  FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
REVOKE ALL ON FUNCTION memory.enqueue_owner_v5_2_stance_schema_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_stance_schema_reextract_v1(uuid)
  TO brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION memory.enqueue_owner_v5_2_stance_schema_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) TO brains_app;

COMMIT;
