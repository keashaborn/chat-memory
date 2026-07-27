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
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL THEN
    RAISE EXCEPTION
      'contextual pet re-extraction prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(
  p_manifest_sha256 text
)
RETURNS TABLE(
  apply_outcome text,
  job_count integer,
  terminal_count integer,
  event_count integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  target record;
  evidence_record memory.evidence%ROWTYPE;
  prior_job memory.evidence_extraction_job%ROWTYPE;
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  existing_event memory.evidence_extraction_event%ROWTYPE;
  decision_fingerprint text;
  inserted_jobs integer := 0;
  inserted_terminals integer := 0;
  inserted_events integer := 0;
  selector constant text :=
    '20260727_v5_2_contextual_pet_reextract_v1';
  manifest_sha constant text :=
    '7fdfbf9e8b07e482f5b25a3f8b49353fdd393727d78e93118c27415e1ef37566';
  provider_source_sha constant text :=
    '6c04a8243f1c0281776f05e90443f3dec1cd2451d5d2238e4c39391baf20fe21';
  relationship_source_sha constant text :=
    '767a4736fd4c4c97b33f01c38652c15c9cc9f574c1e7c1bc8d7a42d5946cc0d6';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION
      'contextual pet re-extraction requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
     OR p_manifest_sha256 IS DISTINCT FROM manifest_sha THEN
    RAISE EXCEPTION
      'contextual pet re-extraction owner or manifest changed'
      USING ERRCODE='23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,selector,manifest_sha),0
  ));

  IF (
    SELECT count(*)
    FROM memory.evidence_extraction_job AS job
    WHERE job.owner_user_id=actor
      AND job.selector_version=selector
  )>0 THEN
    IF (
      SELECT count(*)
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.selector_version=selector
    )<>5
    OR (
      SELECT count(*)
      FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.selector_version=selector
        AND terminal.details->>'manifest_sha256'=manifest_sha
        AND terminal.details->>'provider_source_sha256'=provider_source_sha
        AND terminal.details->>'relationship_source_sha256'=
          relationship_source_sha
    )<>5
    OR (
      SELECT count(*)
      FROM memory.evidence_extraction_event AS event
      JOIN memory.evidence_extraction_job AS job
        ON job.owner_user_id=event.owner_user_id
       AND job.job_id=event.job_id
      WHERE event.owner_user_id=actor
        AND job.selector_version=selector
        AND event.event_type='queued'
        AND event.from_status IS NULL
        AND event.to_status='pending'
        AND event.actor_type='system'
        AND event.actor_ref=
          'memory_v1_v5_2_contextual_pet_reextract_v1'
    )<>5 THEN
      RAISE EXCEPTION
        'contextual pet re-extraction partial replay conflict'
        USING ERRCODE='23514';
    END IF;

    FOR target IN
      SELECT * FROM (VALUES
        (
          '03323af1-c5b1-509a-81be-31f996636cc5'::uuid,
          '73a1dfdc-0825-5a49-b11e-3128ab47c21b'::uuid,
          '94ef9981-59fd-5af4-9689-984c7a87344a'::uuid,
          'ef900a16-00ee-5966-9d39-cf092cead72e'::uuid
        ),
        (
          '0522d532-6b88-5d00-9980-6ea24d0b1af4'::uuid,
          '9f389ae0-6568-5e50-a4a1-dd221e853dd9'::uuid,
          'f8669e3b-2871-5d78-b428-19286ed3d531'::uuid,
          '837f0ad5-e286-527d-9d6f-4b1ca78f4b8a'::uuid
        ),
        (
          '2919855e-cf22-5095-93b4-a881e93b54c0'::uuid,
          'b59d9f7e-ae73-5665-bef1-7ce20a5966ba'::uuid,
          '4ebcd1cf-90cb-534f-b5e0-a1b954832ab3'::uuid,
          '22ccfe9b-92dd-5185-b0da-470b0738a301'::uuid
        ),
        (
          '3e59b50f-8e5f-5e65-a487-72f5d482a19c'::uuid,
          '8100707a-7bd4-5182-be46-df863193e19b'::uuid,
          'd20968db-d9a5-532b-bf3f-240cefd4ad34'::uuid,
          'f2af381e-4695-52a2-a2b2-3ee750588a82'::uuid
        ),
        (
          '5480aacd-e5f3-5060-8c6a-95ef0e46c9fe'::uuid,
          '566735eb-68f2-5091-8689-ac2530eb2d7b'::uuid,
          'acd15144-3711-5348-a98c-a4a2a7c26aa7'::uuid,
          '83ef7538-d851-5def-bbc8-aa30a9d3544c'::uuid
        )
      ) AS value(evidence_id,job_id,terminal_id,operation_id)
    LOOP
      SELECT job.* INTO existing_job
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.evidence_id=target.evidence_id
        AND job.selector_version=selector;
      SELECT terminal.* INTO existing_terminal
      FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.evidence_id=target.evidence_id
        AND terminal.selector_version=selector;
      SELECT event.* INTO existing_event
      FROM memory.evidence_extraction_event AS event
      WHERE event.owner_user_id=actor
        AND event.operation_id=target.operation_id;
      IF existing_job.job_id IS DISTINCT FROM target.job_id
         OR existing_terminal.terminal_id IS DISTINCT FROM target.terminal_id
         OR existing_job.intake_terminal_id
            IS DISTINCT FROM target.terminal_id
         OR existing_event.job_id IS DISTINCT FROM target.job_id
         OR existing_terminal.details->>'manifest_sha256'
            IS DISTINCT FROM manifest_sha THEN
        RAISE EXCEPTION
          'contextual pet re-extraction replay binding changed'
          USING ERRCODE='23514';
      END IF;
    END LOOP;

    RETURN QUERY SELECT 'replayed'::text,5,5,5;
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory.evidence_intake_terminal AS terminal
    WHERE terminal.owner_user_id=actor
      AND terminal.selector_version=selector
  ) OR EXISTS (
    SELECT 1 FROM memory.evidence_extraction_event AS event
    WHERE event.owner_user_id=actor
      AND event.operation_id IN (
        'ef900a16-00ee-5966-9d39-cf092cead72e'::uuid,
        '837f0ad5-e286-527d-9d6f-4b1ca78f4b8a'::uuid,
        '22ccfe9b-92dd-5185-b0da-470b0738a301'::uuid,
        'f2af381e-4695-52a2-a2b2-3ee750588a82'::uuid,
        '83ef7538-d851-5def-bbc8-aa30a9d3544c'::uuid
      )
  ) THEN
    RAISE EXCEPTION
      'contextual pet re-extraction identifier collision'
      USING ERRCODE='23514';
  END IF;

  FOR target IN
    SELECT * FROM (VALUES
      (
        '03323af1-c5b1-509a-81be-31f996636cc5'::uuid,
        '6df993f62a9d749c100dc481fa661700cc48bfca50d63c2aba8cde1fe7b79082',
        '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
        'review_required',2,
        'ef900a16-00ee-5966-9d39-cf092cead72e'::uuid,
        '73a1dfdc-0825-5a49-b11e-3128ab47c21b'::uuid,
        '94ef9981-59fd-5af4-9689-984c7a87344a'::uuid
      ),
      (
        '0522d532-6b88-5d00-9980-6ea24d0b1af4'::uuid,
        '9c6993e8aff11eae0b6d2235346055f33b72117c0f89d6e256f2ca8633235912',
        '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
        'pending',1,
        '837f0ad5-e286-527d-9d6f-4b1ca78f4b8a'::uuid,
        '9f389ae0-6568-5e50-a4a1-dd221e853dd9'::uuid,
        'f8669e3b-2871-5d78-b428-19286ed3d531'::uuid
      ),
      (
        '2919855e-cf22-5095-93b4-a881e93b54c0'::uuid,
        'e669f3377fb0f3936199de063d04375deaa519202b69c20de863f68e6ef9956d',
        'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
        'pending',1,
        '22ccfe9b-92dd-5185-b0da-470b0738a301'::uuid,
        'b59d9f7e-ae73-5665-bef1-7ce20a5966ba'::uuid,
        '4ebcd1cf-90cb-534f-b5e0-a1b954832ab3'::uuid
      ),
      (
        '3e59b50f-8e5f-5e65-a487-72f5d482a19c'::uuid,
        'e56a7f11175bf0c0db2335d93581fbbc77c86402a341e51baf112e95743de7ec',
        '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
        'pending',1,
        'f2af381e-4695-52a2-a2b2-3ee750588a82'::uuid,
        '8100707a-7bd4-5182-be46-df863193e19b'::uuid,
        'd20968db-d9a5-532b-bf3f-240cefd4ad34'::uuid
      ),
      (
        '5480aacd-e5f3-5060-8c6a-95ef0e46c9fe'::uuid,
        '8e4cfbd41156ccc92fd4392372a220d3e5802597c6d5756f8d48748503d0166b',
        '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid,
        'pending',1,
        '83ef7538-d851-5def-bbc8-aa30a9d3544c'::uuid,
        '566735eb-68f2-5091-8689-ac2530eb2d7b'::uuid,
        'acd15144-3711-5348-a98c-a4a2a7c26aa7'::uuid
      )
    ) AS value(
      evidence_id,content_sha256,prior_job_id,prior_status,prior_attempts,
      operation_id,job_id,terminal_id
    )
  LOOP
    SELECT evidence.* INTO evidence_record
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND evidence.evidence_id=target.evidence_id;
    IF NOT FOUND
       OR evidence_record.status<>'active'
       OR evidence_record.source_system<>'public.chat_log'
       OR evidence_record.content_sha256
          IS DISTINCT FROM target.content_sha256 THEN
      RAISE EXCEPTION
        'contextual pet re-extraction evidence changed'
        USING ERRCODE='23514';
    END IF;

    SELECT job.* INTO prior_job
    FROM memory.evidence_extraction_job AS job
    WHERE job.owner_user_id=actor
      AND job.job_id=target.prior_job_id
      AND job.evidence_id=target.evidence_id;
    IF NOT FOUND
       OR prior_job.status::text IS DISTINCT FROM target.prior_status
       OR prior_job.attempts IS DISTINCT FROM target.prior_attempts
       OR prior_job.selector_version<>'20260717_v2'
       OR prior_job.route<>'relational_extraction'
       OR prior_job.evidence_content_sha256
          IS DISTINCT FROM target.content_sha256
       OR prior_job.lease_token IS NOT NULL
       OR prior_job.lease_expires_at IS NOT NULL
       OR prior_job.last_error IS NOT NULL THEN
      RAISE EXCEPTION
        'contextual pet re-extraction prior job changed'
        USING ERRCODE='23514';
    END IF;

    IF EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=target.evidence_id
    ) OR EXISTS (
      SELECT 1 FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND (
          job.job_id=target.job_id
          OR (
            job.evidence_id=target.evidence_id
            AND job.selector_version=selector
          )
        )
    ) OR EXISTS (
      SELECT 1 FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id=actor
        AND (
          terminal.terminal_id=target.terminal_id
          OR (
            terminal.evidence_id=target.evidence_id
            AND terminal.selector_version=selector
          )
        )
    ) THEN
      RAISE EXCEPTION
        'contextual pet re-extraction target conflict'
        USING ERRCODE='23514';
    END IF;

    decision_fingerprint := encode(public.digest(convert_to(
      jsonb_build_object(
        'contract_version',
          'memory_v1_v5_2_contextual_pet_reextract_v1',
        'owner_user_id',actor,
        'operation_id',target.operation_id,
        'job_id',target.job_id,
        'terminal_id',target.terminal_id,
        'evidence_id',target.evidence_id,
        'evidence_content_sha256',target.content_sha256,
        'selector_version',selector,
        'manifest_sha256',manifest_sha,
        'provider_source_sha256',provider_source_sha,
        'relationship_source_sha256',relationship_source_sha,
        'prior_job_id',target.prior_job_id,
        'prior_status',target.prior_status,
        'prior_attempts',target.prior_attempts
      )::text,'UTF8'),'sha256'),'hex');

    INSERT INTO memory.evidence_intake_terminal(
      terminal_id,owner_user_id,evidence_id,selector_version,
      outcome,reason_code,evidence_content_sha256,
      decision_fingerprint,actor_user_id,invoked_by_role,details
    ) VALUES (
      target.terminal_id,actor,target.evidence_id,selector,
      'dispatched','eligible_dispatched',target.content_sha256,
      decision_fingerprint,actor,session_user,
      jsonb_build_object(
        'route','relational_extraction',
        'extraction_job_id',target.job_id,
        'reextract_contract',
          'memory_v1_v5_2_contextual_pet_reextract_v1',
        'operation_id',target.operation_id,
        'manifest_sha256',manifest_sha,
        'provider_source_sha256',provider_source_sha,
        'relationship_source_sha256',relationship_source_sha,
        'prior_job_id',target.prior_job_id,
        'prior_status',target.prior_status,
        'prior_attempts',target.prior_attempts,
        'source_prose_copied',false
      )
    );
    inserted_terminals := inserted_terminals+1;

    INSERT INTO memory.evidence_extraction_job(
      job_id,owner_user_id,evidence_id,intake_terminal_id,
      selector_version,evidence_content_sha256,route,
      intake_reason_code,status
    ) VALUES (
      target.job_id,actor,target.evidence_id,target.terminal_id,
      selector,target.content_sha256,'relational_extraction',
      'eligible_unprocessed','pending'
    );
    inserted_jobs := inserted_jobs+1;

    INSERT INTO memory.evidence_extraction_event(
      owner_user_id,job_id,operation_id,event_type,
      from_status,to_status,actor_type,actor_ref,details
    ) VALUES (
      actor,target.job_id,target.operation_id,'queued',
      NULL,'pending','system',
      'memory_v1_v5_2_contextual_pet_reextract_v1',
      jsonb_build_object(
        'intake_terminal_id',target.terminal_id,
        'selector_version',selector,
        'route','relational_extraction',
        'manifest_sha256',manifest_sha,
        'provider_source_sha256',provider_source_sha,
        'relationship_source_sha256',relationship_source_sha,
        'prior_job_id',target.prior_job_id
      )
    );
    inserted_events := inserted_events+1;
  END LOOP;

  IF inserted_jobs<>5 OR inserted_terminals<>5 OR inserted_events<>5 THEN
    RAISE EXCEPTION
      'contextual pet re-extraction bounded row count changed'
      USING ERRCODE='23514';
  END IF;

  RETURN QUERY SELECT
    'applied'::text,inserted_jobs,inserted_terminals,inserted_events;
END
$function$;

ALTER FUNCTION
memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(text)
OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(text)
FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;

GRANT EXECUTE ON FUNCTION
memory.enqueue_owner_v5_2_contextual_pet_reextract_v1(text)
TO brains_app;

COMMIT;
