BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('public.digest(bytea,text)') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_ingest_batch_row') IS NULL
     OR to_regclass('memory.candidate') IS NULL
     OR to_regclass('memory.claim_evidence') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL THEN
    RAISE EXCEPTION 'compiler-v7 review re-extraction prerequisites are absent';
  END IF;
END
$preflight$;

GRANT SELECT ON
  memory.evidence_ingest_batch_row,
  memory.candidate,
  memory.claim_evidence
TO memory_v5_local_reextract_maintainer;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_terminal_id uuid,
  p_evidence_id uuid,
  p_expected_content_sha256 text,
  p_manifest_sha256 text,
  p_expected_policy_compiler_sha256 text
)
RETURNS TABLE(
  job_id uuid,
  terminal_id uuid,
  status text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  selector constant text := '20260724_v5_2_compiler_v7_review_v1';
  compiler_sha constant text :=
    'a4387aad59445f65fdfc8413f48567b8f560a352cb6fcf8de415769b8752bfbc';
  evidence_record memory.evidence%ROWTYPE;
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  decision_fingerprint text;
  prior_accounting boolean;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION
      'compiler-v7 review re-extraction requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_job_id IS NULL
     OR p_terminal_id IS NULL
     OR p_evidence_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_policy_compiler_sha256 IS DISTINCT FROM compiler_sha THEN
    RAISE EXCEPTION 'compiler-v7 review re-extraction inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,p_evidence_id::text,selector),0
  ));

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_evidence_id;
  IF NOT FOUND
     OR evidence_record.status<>'active'
     OR evidence_record.source_system<>'public.chat_log'
     OR evidence_record.content_sha256
        IS DISTINCT FROM p_expected_content_sha256 THEN
    RAISE EXCEPTION 'compiler-v7 review re-extraction evidence changed'
      USING ERRCODE='23514';
  END IF;

  prior_accounting :=
    EXISTS (
      SELECT 1
      FROM memory.evidence_ingest_batch_row AS link
      WHERE link.owner_user_id=actor
        AND link.evidence_id=p_evidence_id
    )
    OR EXISTS (
      SELECT 1
      FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.evidence_id=p_evidence_id
    )
    OR EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.evidence_id=p_evidence_id
    )
    OR EXISTS (
      SELECT 1
      FROM memory.candidate AS link
      WHERE link.owner_user_id=actor
        AND link.evidence_id=p_evidence_id
    )
    OR EXISTS (
      SELECT 1
      FROM memory.claim_evidence AS link
      WHERE link.owner_user_id=actor
        AND link.evidence_id=p_evidence_id
    );
  IF NOT prior_accounting THEN
    RAISE EXCEPTION
      'compiler-v7 review re-extraction requires prior governed accounting'
      USING ERRCODE='23514';
  END IF;

  SELECT job.* INTO existing_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor
    AND job.evidence_id=p_evidence_id
    AND job.selector_version=selector;

  SELECT terminal.* INTO existing_terminal
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id=actor
    AND terminal.evidence_id=p_evidence_id
    AND terminal.selector_version=selector;

  IF existing_job.job_id IS NOT NULL
     OR existing_terminal.terminal_id IS NOT NULL THEN
    IF existing_job.job_id IS DISTINCT FROM p_job_id
       OR existing_terminal.terminal_id IS DISTINCT FROM p_terminal_id
       OR existing_job.intake_terminal_id
          IS DISTINCT FROM existing_terminal.terminal_id
       OR existing_job.evidence_content_sha256
          IS DISTINCT FROM p_expected_content_sha256
       OR existing_terminal.evidence_content_sha256
          IS DISTINCT FROM p_expected_content_sha256
       OR existing_job.route<>'relational_extraction'
       OR existing_job.intake_reason_code<>'eligible_unprocessed'
       OR existing_terminal.outcome<>'dispatched'
       OR existing_terminal.reason_code<>'eligible_dispatched'
       OR existing_terminal.details->>'reextract_contract'
          IS DISTINCT FROM
            'memory_v1_v5_2_compiler_v7_review_reextract_v1'
       OR existing_terminal.details->>'manifest_sha256'
          IS DISTINCT FROM p_manifest_sha256
       OR existing_terminal.details->>'policy_compiler_sha256'
          IS DISTINCT FROM compiler_sha THEN
      RAISE EXCEPTION
        'compiler-v7 review re-extraction replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing_job.job_id,
      existing_terminal.terminal_id,existing_job.status::text,'replayed'::text;
    RETURN;
  END IF;

  IF EXISTS (
       SELECT 1 FROM memory.evidence_extraction_job AS collision_job
       WHERE collision_job.job_id=p_job_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.evidence_intake_terminal AS collision_terminal
       WHERE collision_terminal.terminal_id=p_terminal_id
     ) THEN
    RAISE EXCEPTION 'compiler-v7 review re-extraction id collision'
      USING ERRCODE='23514';
  END IF;

  decision_fingerprint := encode(public.digest(convert_to(
    jsonb_build_object(
      'contract_version',
        'memory_v1_v5_2_compiler_v7_review_reextract_v1',
      'owner_user_id',actor,
      'operation_id',p_operation_id,
      'job_id',p_job_id,
      'terminal_id',p_terminal_id,
      'evidence_id',p_evidence_id,
      'evidence_content_sha256',p_expected_content_sha256,
      'selector_version',selector,
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha
    )::text,'UTF8'),'sha256'),'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,
    outcome,reason_code,evidence_content_sha256,
    decision_fingerprint,actor_user_id,invoked_by_role,details
  ) VALUES (
    p_terminal_id,actor,p_evidence_id,selector,
    'dispatched','eligible_dispatched',p_expected_content_sha256,
    decision_fingerprint,actor,session_user,
    jsonb_build_object(
      'route','relational_extraction',
      'extraction_job_id',p_job_id,
      'plan_reason_code','eligible_unprocessed',
      'reextract_contract',
        'memory_v1_v5_2_compiler_v7_review_reextract_v1',
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,owner_user_id,evidence_id,intake_terminal_id,
    selector_version,evidence_content_sha256,route,
    intake_reason_code,status
  ) VALUES (
    p_job_id,actor,p_evidence_id,p_terminal_id,
    selector,p_expected_content_sha256,'relational_extraction',
    'eligible_unprocessed','pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,
    from_status,to_status,actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'queued',
    NULL,'pending','system',
    'memory_v1_v5_2_compiler_v7_review_reextract_v1',
    jsonb_build_object(
      'intake_terminal_id',p_terminal_id,
      'selector_version',selector,
      'route','relational_extraction',
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha
    )
  );

  RETURN QUERY SELECT p_job_id,p_terminal_id,'pending'::text,'applied'::text;
END
$function$;

ALTER FUNCTION
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;

GRANT EXECUTE ON FUNCTION
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text
) TO brains_app;

COMMIT;
