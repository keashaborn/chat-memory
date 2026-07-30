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
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  p_operation_id uuid,
  p_job_id uuid,
  p_terminal_id uuid,
  p_evidence_id uuid,
  p_expected_content_sha256 text,
  p_prior_packet_id uuid,
  p_expected_prior_packet_storage_sha256 text,
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
  selector constant text :=
    '20260730_v5_2_semantic_compiler_v9_reextract_v1';
  compiler_sha constant text :=
    '738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6';
  prior_compiler_sha constant text :=
    'f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419';
  allowed_content_sha text;
  allowed_prior_packet_id uuid;
  allowed_prior_packet_storage_sha text;
  evidence_record memory.evidence%ROWTYPE;
  prior_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  prior_job memory.evidence_extraction_job%ROWTYPE;
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  decision_fingerprint text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction requires brains_app session'
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
     OR p_prior_packet_id IS NULL
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_prior_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_policy_compiler_sha256 IS DISTINCT FROM compiler_sha THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  CASE p_evidence_id
    WHEN '541d60a4-3486-4e41-a34b-aad653d4ac89'::uuid THEN
      allowed_content_sha :=
        '2a3e2770b0eea1a587c9dd14fafd6a4cc62906a02792849566468cc0a31d2cfd';
      allowed_prior_packet_id :=
        '0f5fca46-1885-5c5d-aa9a-bb814ed56e43'::uuid;
      allowed_prior_packet_storage_sha :=
        '41ad10f8b1be318b6d59e4d408ef00d811db70a0173cba8eec4654e04d4b0e68';
    WHEN '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid THEN
      allowed_content_sha :=
        '966b7a3d4c47e77bae6b7eaea87d8c8a88d268d3d48dcf3fa9c20dbca3dc41e8';
      allowed_prior_packet_id :=
        '083917f3-d95d-5408-a7e0-358997d28444'::uuid;
      allowed_prior_packet_storage_sha :=
        'e8d39e7ae945d9b0f2ae9bc0587ee536a2b9953756fc0a8e38cff13dd4dc56c6';
    WHEN '6ce40aba-1e7c-589e-87cd-cb8b028bce8f'::uuid THEN
      allowed_content_sha :=
        'c5ceb3f3008c9308b61e4244f9203c44ddf37a74f5bd65714907c4eef7a1fd73';
      allowed_prior_packet_id :=
        '0b3b9f75-b141-5028-99c0-132e3dc27499'::uuid;
      allowed_prior_packet_storage_sha :=
        '5e942fb90b770d51a76896a895570177ec6d534ae5e2f39c160d361341d7c187';
    WHEN '8f3f097c-34e7-4fdf-a492-35addb2ab8f5'::uuid THEN
      allowed_content_sha :=
        '48df291f0de8b18aba122e0ed0db915f0c98d3de33f478ff41822899d62c2977';
      allowed_prior_packet_id :=
        'ea8f9293-2c08-5713-a56e-2d5ab592f562'::uuid;
      allowed_prior_packet_storage_sha :=
        'aa27d3b6bc059100f6782e1d3e7177bbb75b4c40a16b421f4c73cdcbabe7f31f';
    WHEN 'a6b5a5ad-6756-540d-b83a-6f25ef4d073b'::uuid THEN
      allowed_content_sha :=
        '3bdca19ebc481a49ab7c7e00510e1d178b31ebf73fc7d0e2165cfc3e7bab06bf';
      allowed_prior_packet_id :=
        '296d260b-c0a8-5cf5-9861-ff587ad70b7d'::uuid;
      allowed_prior_packet_storage_sha :=
        'e6dcdfd316dadb1455adf3aa5b613df5420911572b1f03d9a45860526aca14df';
    WHEN 'ad0aeacd-f419-592b-94a6-94693399cd8c'::uuid THEN
      allowed_content_sha :=
        '841bcbb1937b5b6f610bb81a769ee62954aff803e125b572884091c50fa07ad2';
      allowed_prior_packet_id :=
        '21969e61-ab42-5058-831d-6ca3ba865d3d'::uuid;
      allowed_prior_packet_storage_sha :=
        'd9f4cdd8462aec43ffaa734386a0844ce60c7d095ba843000b9ab31b4cc2888a';
    WHEN 'f2db61f9-ec1a-5694-8e18-5ca5953aecce'::uuid THEN
      allowed_content_sha :=
        '8fe05a934b55a77ac6f34945a1e3d9f9e75d5a996c6e8032bc9ac506ef3d1a13';
      allowed_prior_packet_id :=
        'fa1231ed-b88c-5283-b234-d36b7904cea0'::uuid;
      allowed_prior_packet_storage_sha :=
        '096a107b04a864b206cb1fb5d9a56c8501e92dd9d230b5026191b1d96a1e2e28';
    ELSE
      RAISE EXCEPTION
        'evidence is outside the exact semantic compiler-v9 re-extraction set'
        USING ERRCODE='23514';
  END CASE;

  IF p_expected_content_sha256 IS DISTINCT FROM allowed_content_sha
     OR p_prior_packet_id IS DISTINCT FROM allowed_prior_packet_id
     OR p_expected_prior_packet_storage_sha256
        IS DISTINCT FROM allowed_prior_packet_storage_sha THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction binding changed'
      USING ERRCODE='23514';
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
        IS DISTINCT FROM allowed_content_sha THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction evidence changed'
      USING ERRCODE='23514';
  END IF;

  SELECT packet.* INTO prior_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=allowed_prior_packet_id
    AND packet.evidence_id=p_evidence_id;
  IF NOT FOUND
     OR prior_packet.evidence_content_sha256
        IS DISTINCT FROM allowed_content_sha
     OR prior_packet.packet_storage_sha256
        IS DISTINCT FROM allowed_prior_packet_storage_sha
     OR prior_packet.policy_compiler_sha256
        IS DISTINCT FROM prior_compiler_sha THEN
    RAISE EXCEPTION
      'semantic compiler-v9 prior packet changed'
      USING ERRCODE='23514';
  END IF;

  SELECT job.* INTO prior_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor
    AND job.job_id=prior_packet.job_id
    AND job.evidence_id=p_evidence_id;
  IF NOT FOUND
     OR prior_job.status<>'review_required'
     OR prior_job.selector_version
        <>'20260717_v2' THEN
    RAISE EXCEPTION
      'semantic compiler-v9 prior review state changed'
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
          IS DISTINCT FROM allowed_content_sha
       OR existing_terminal.evidence_content_sha256
          IS DISTINCT FROM allowed_content_sha
       OR existing_job.route<>'relational_extraction'
       OR existing_job.intake_reason_code<>'eligible_unprocessed'
       OR existing_terminal.outcome<>'dispatched'
       OR existing_terminal.reason_code<>'eligible_dispatched'
       OR existing_terminal.details->>'reextract_contract'
          IS DISTINCT FROM
            'memory_v1_v5_2_semantic_compiler_v9_reextract_v1'
       OR existing_terminal.details->>'operation_id'
          IS DISTINCT FROM p_operation_id::text
       OR existing_terminal.details->>'manifest_sha256'
          IS DISTINCT FROM p_manifest_sha256
       OR existing_terminal.details->>'policy_compiler_sha256'
          IS DISTINCT FROM compiler_sha
       OR existing_terminal.details->>'prior_packet_id'
          IS DISTINCT FROM allowed_prior_packet_id::text
       OR existing_terminal.details->>'prior_packet_storage_sha256'
          IS DISTINCT FROM allowed_prior_packet_storage_sha THEN
      RAISE EXCEPTION
        'semantic compiler-v9 re-extraction replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing_job.job_id,
      existing_terminal.terminal_id,existing_job.status::text,
      'replayed'::text;
    RETURN;
  END IF;

  IF EXISTS (
       SELECT 1 FROM memory.evidence_extraction_job AS collision_job
       WHERE collision_job.job_id=p_job_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.evidence_intake_terminal AS collision_terminal
       WHERE collision_terminal.terminal_id=p_terminal_id
     )
     OR EXISTS (
       SELECT 1 FROM memory.evidence_extraction_event AS collision_event
       WHERE collision_event.operation_id=p_operation_id
     ) THEN
    RAISE EXCEPTION
      'semantic compiler-v9 re-extraction id collision'
      USING ERRCODE='23514';
  END IF;

  decision_fingerprint := encode(public.digest(convert_to(
    jsonb_build_object(
      'contract_version',
        'memory_v1_v5_2_semantic_compiler_v9_reextract_v1',
      'owner_user_id',actor,
      'operation_id',p_operation_id,
      'job_id',p_job_id,
      'terminal_id',p_terminal_id,
      'evidence_id',p_evidence_id,
      'evidence_content_sha256',allowed_content_sha,
      'selector_version',selector,
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha,
      'prior_packet_id',allowed_prior_packet_id,
      'prior_packet_storage_sha256',allowed_prior_packet_storage_sha
    )::text,'UTF8'),'sha256'),'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,
    outcome,reason_code,evidence_content_sha256,
    decision_fingerprint,actor_user_id,invoked_by_role,details
  ) VALUES (
    p_terminal_id,actor,p_evidence_id,selector,
    'dispatched','eligible_dispatched',allowed_content_sha,
    decision_fingerprint,actor,session_user,
    jsonb_build_object(
      'route','relational_extraction',
      'extraction_job_id',p_job_id,
      'plan_reason_code','eligible_unprocessed',
      'reextract_contract',
        'memory_v1_v5_2_semantic_compiler_v9_reextract_v1',
      'operation_id',p_operation_id,
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha,
      'prior_packet_id',allowed_prior_packet_id,
      'prior_packet_storage_sha256',allowed_prior_packet_storage_sha
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,owner_user_id,evidence_id,intake_terminal_id,
    selector_version,evidence_content_sha256,route,
    intake_reason_code,status
  ) VALUES (
    p_job_id,actor,p_evidence_id,p_terminal_id,
    selector,allowed_content_sha,'relational_extraction',
    'eligible_unprocessed','pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,operation_id,event_type,
    from_status,to_status,actor_type,actor_ref,details
  ) VALUES (
    actor,p_job_id,p_operation_id,'queued',
    NULL,'pending','system',
    'memory_v1_v5_2_semantic_compiler_v9_reextract_v1',
    jsonb_build_object(
      'intake_terminal_id',p_terminal_id,
      'selector_version',selector,
      'route','relational_extraction',
      'manifest_sha256',p_manifest_sha256,
      'policy_compiler_sha256',compiler_sha,
      'prior_packet_id',allowed_prior_packet_id,
      'prior_packet_storage_sha256',allowed_prior_packet_storage_sha
    )
  );

  RETURN QUERY SELECT p_job_id,p_terminal_id,'pending'::text,'applied'::text;
END
$function$;

ALTER FUNCTION
memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  uuid,uuid,uuid,uuid,text,uuid,text,text,text
) OWNER TO memory_v5_local_reextract_maintainer;

REVOKE ALL ON FUNCTION
memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  uuid,uuid,uuid,uuid,text,uuid,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;

GRANT EXECUTE ON FUNCTION
memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  uuid,uuid,uuid,uuid,text,uuid,text,text,text
) TO brains_app;

COMMIT;
