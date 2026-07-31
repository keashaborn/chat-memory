BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'compiler-v12 exact-two prerequisites are absent';
  END IF;
END
$preflight$;

GRANT SELECT ON memory.v5_2_local_packet_route_event
  TO memory_v5_local_reextract_maintainer;

DROP POLICY IF EXISTS v5_2_compiler_v12_exact_two_route_read
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY v5_2_compiler_v12_exact_two_route_read
ON memory.v5_2_local_packet_route_event
FOR SELECT TO memory_v5_local_reextract_maintainer
USING (owner_user_id=memory.current_actor_user_id());

CREATE OR REPLACE FUNCTION
memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(
  p_manifest jsonb,
  p_manifest_sha256 text,
  p_expected_policy_compiler_sha256 text
)
RETURNS TABLE(
  evidence_id uuid,
  job_id uuid,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  target record;
  existing memory.evidence_extraction_job%ROWTYPE;
  new_job_id uuid;
  new_terminal_id uuid;
  new_operation_id uuid;
  decision_fingerprint text;
  expected_observed_at timestamptz;
  seen_stance boolean:=false;
  seen_jerry boolean:=false;
  selector constant text :=
    '20260731_v5_2_compiler_v12_exact_two_v1';
  manifest_sha constant text :=
    '13682f6959b491f1ab8c0e90cd54843c6164cc9b202b0683ccda53c430fe2052';
  compiler_sha constant text :=
    '91e3830e676b6c9abd881a52f3d6b010679809c86fffbc889134d9653deba5f3';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'compiler-v12 exact-two enqueue requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
     OR p_manifest_sha256 IS DISTINCT FROM manifest_sha
     OR p_expected_policy_compiler_sha256 IS DISTINCT FROM compiler_sha
     OR jsonb_typeof(p_manifest)<>'object'
     OR p_manifest->>'contract_version'<>
        'memory_v1_v5_2_compiler_v12_exact_two_v1'
     OR p_manifest->>'owner_user_id'<>actor::text
     OR p_manifest->>'selector_version'<>selector
     OR p_manifest->>'policy_compiler_sha256'<>compiler_sha
     OR jsonb_typeof(p_manifest->'targets')<>'array'
     OR jsonb_array_length(p_manifest->'targets')<>2 THEN
    RAISE EXCEPTION 'compiler-v12 exact-two manifest is invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,selector,manifest_sha),0
  ));

  FOR target IN
    SELECT *
    FROM jsonb_to_recordset(p_manifest->'targets') AS item(
      evidence_id uuid,
      content_sha256 text,
      prior_packet_id uuid,
      prior_packet_storage_sha256 text,
      prior_policy_compiler_sha256 text,
      prior_selector_version text,
      expected_route text,
      max_local_model_calls integer
    )
    ORDER BY item.evidence_id
  LOOP
    CASE target.evidence_id
      WHEN '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid THEN
        IF seen_stance
           OR target.content_sha256<>
             '966b7a3d4c47e77bae6b7eaea87d8c8a88d268d3d48dcf3fa9c20dbca3dc41e8'
           OR target.prior_packet_id<>
             '50405677-84aa-5d72-b800-89418fc606e4'::uuid
           OR target.prior_packet_storage_sha256<>
             '6276f9c4cf03ee8276b96451e0c7915691ebfeb459036123a5f0fa977c826cbc'
           OR target.prior_policy_compiler_sha256<>
             '738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6'
           OR target.prior_selector_version<>
             '20260730_v5_2_semantic_compiler_v9_reextract_v1'
           OR target.expected_route<>'manual_review_artifact_ready'
           OR target.max_local_model_calls<>1 THEN
          RAISE EXCEPTION 'compiler-v12 stance target contract changed'
            USING ERRCODE='23514';
        END IF;
        seen_stance:=true;
        expected_observed_at:='2026-07-18 19:40:09.461819+00';
      WHEN '681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid THEN
        IF seen_jerry
           OR target.content_sha256<>
             '46f40455eba48cdd0c4e131cc8f01ea15d8721b761bf0e15365d008a0562ea93'
           OR target.prior_packet_id<>
             'c4db1405-ad9d-5c9a-81e3-10ad0200b0ca'::uuid
           OR target.prior_packet_storage_sha256<>
             '90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d'
           OR target.prior_policy_compiler_sha256<>
             '275b5150f42e6d90e0afcebe86b32ab691be2e682c4d0c74e61947652230f6a2'
           OR target.prior_selector_version<>
             '20260731_v5_2_semantic_compiler_v11_jerry_v1'
           OR target.expected_route<>'manual_review_artifact_ready'
           OR target.max_local_model_calls<>0 THEN
          RAISE EXCEPTION 'compiler-v12 Jerry target contract changed'
            USING ERRCODE='23514';
        END IF;
        seen_jerry:=true;
        expected_observed_at:='2026-07-30 21:39:53.840736+00';
      ELSE
        RAISE EXCEPTION 'evidence is outside compiler-v12 exact-two scope'
          USING ERRCODE='23514';
    END CASE;

    SELECT job.* INTO existing
    FROM memory.evidence_extraction_job AS job
    WHERE job.owner_user_id=actor
      AND job.selector_version=selector
      AND job.evidence_id=target.evidence_id;
    IF FOUND THEN
      IF existing.evidence_content_sha256<>target.content_sha256
         OR existing.route<>'relational_extraction'
         OR existing.status NOT IN (
           'pending','processing','review_required','completed','skipped'
         )
         OR NOT EXISTS (
           SELECT 1 FROM memory.evidence_intake_terminal AS terminal
           WHERE terminal.owner_user_id=actor
             AND terminal.terminal_id=existing.intake_terminal_id
             AND terminal.evidence_id=target.evidence_id
             AND terminal.selector_version=selector
             AND terminal.outcome='dispatched'
             AND terminal.reason_code='eligible_dispatched'
             AND terminal.details->>'manifest_sha256'=manifest_sha
             AND terminal.details->>'policy_compiler_sha256'=compiler_sha
             AND terminal.details->>'prior_packet_id'=
                 target.prior_packet_id::text
             AND terminal.details->>'prior_packet_storage_sha256'=
                 target.prior_packet_storage_sha256
         ) THEN
        RAISE EXCEPTION 'compiler-v12 exact-two replay conflicts'
          USING ERRCODE='23514';
      END IF;
      evidence_id:=target.evidence_id;
      job_id:=existing.job_id;
      apply_outcome:='replayed';
      RETURN NEXT;
      CONTINUE;
    END IF;

    IF NOT EXISTS (
      SELECT 1
      FROM memory.evidence AS evidence
      JOIN memory.evidence_extraction_packet_v5_local AS packet
        ON packet.owner_user_id=evidence.owner_user_id
       AND packet.evidence_id=evidence.evidence_id
      JOIN memory.evidence_extraction_job AS prior_job
        ON prior_job.owner_user_id=packet.owner_user_id
       AND prior_job.job_id=packet.job_id
      JOIN memory.v5_2_local_packet_route_event AS route
        ON route.owner_user_id=packet.owner_user_id
       AND route.packet_id=packet.packet_id
      WHERE evidence.owner_user_id=actor
        AND evidence.evidence_id=target.evidence_id
        AND evidence.content_sha256=target.content_sha256
        AND evidence.status='active'
        AND evidence.observed_at=expected_observed_at
        AND packet.packet_id=target.prior_packet_id
        AND packet.packet_storage_sha256=
            target.prior_packet_storage_sha256
        AND packet.policy_compiler_sha256=
            target.prior_policy_compiler_sha256
        AND prior_job.selector_version=target.prior_selector_version
        AND prior_job.status='review_required'
        AND prior_job.route='relational_extraction'
        AND route.route=target.expected_route
        AND route.reason_code='reviewable_relational_packet_v5_2'
    ) THEN
      RAISE EXCEPTION 'compiler-v12 exact-two production binding changed'
        USING ERRCODE='23514';
    END IF;

    new_job_id:=gen_random_uuid();
    new_terminal_id:=gen_random_uuid();
    new_operation_id:=gen_random_uuid();
    decision_fingerprint:=encode(public.digest(convert_to(
      jsonb_build_object(
        'contract_version','memory_v1_v5_2_compiler_v12_exact_two_v1',
        'owner_user_id',actor,
        'evidence_id',target.evidence_id,
        'content_sha256',target.content_sha256,
        'selector_version',selector,
        'manifest_sha256',manifest_sha,
        'policy_compiler_sha256',compiler_sha,
        'prior_packet_id',target.prior_packet_id,
        'prior_packet_storage_sha256',target.prior_packet_storage_sha256
      )::text,'UTF8'),'sha256'),'hex');

    INSERT INTO memory.evidence_intake_terminal(
      terminal_id,owner_user_id,evidence_id,selector_version,
      outcome,reason_code,evidence_content_sha256,
      decision_fingerprint,actor_user_id,invoked_by_role,details
    ) VALUES (
      new_terminal_id,actor,target.evidence_id,selector,
      'dispatched','eligible_dispatched',target.content_sha256,
      decision_fingerprint,actor,session_user,
      jsonb_build_object(
        'route','relational_extraction',
        'extraction_job_id',new_job_id,
        'plan_reason_code','eligible_unprocessed',
        'reextract_contract','memory_v1_v5_2_compiler_v12_exact_two_v1',
        'manifest_sha256',manifest_sha,
        'policy_compiler_sha256',compiler_sha,
        'prior_packet_id',target.prior_packet_id,
        'prior_packet_storage_sha256',target.prior_packet_storage_sha256,
        'prior_policy_compiler_sha256',target.prior_policy_compiler_sha256,
        'expected_route',target.expected_route,
        'max_local_model_calls',target.max_local_model_calls
      )
    );
    INSERT INTO memory.evidence_extraction_job(
      job_id,owner_user_id,evidence_id,intake_terminal_id,
      selector_version,evidence_content_sha256,route,
      intake_reason_code,status
    ) VALUES (
      new_job_id,actor,target.evidence_id,new_terminal_id,
      selector,target.content_sha256,'relational_extraction',
      'eligible_unprocessed','pending'
    );
    INSERT INTO memory.evidence_extraction_event(
      owner_user_id,job_id,operation_id,event_type,
      from_status,to_status,actor_type,actor_ref,details
    ) VALUES (
      actor,new_job_id,new_operation_id,'queued',NULL,'pending','system',
      'memory_v1_v5_2_compiler_v12_exact_two_v1',
      jsonb_build_object(
        'intake_terminal_id',new_terminal_id,
        'selector_version',selector,
        'manifest_sha256',manifest_sha,
        'policy_compiler_sha256',compiler_sha,
        'prior_packet_id',target.prior_packet_id,
        'prior_packet_storage_sha256',target.prior_packet_storage_sha256
      )
    );
    evidence_id:=target.evidence_id;
    job_id:=new_job_id;
    apply_outcome:='applied';
    RETURN NEXT;
  END LOOP;

  IF NOT seen_stance OR NOT seen_jerry THEN
    RAISE EXCEPTION 'compiler-v12 exact-two target set is incomplete'
      USING ERRCODE='23514';
  END IF;
END
$function$;

ALTER FUNCTION memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(
  jsonb,text,text
) OWNER TO memory_v5_local_reextract_maintainer;
REVOKE ALL ON FUNCTION
memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(jsonb,text,text)
  FROM PUBLIC,brains_app,memory_v5_local_reextract_maintainer;
GRANT EXECUTE ON FUNCTION
memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(jsonb,text,text)
  TO brains_app;

COMMIT;
