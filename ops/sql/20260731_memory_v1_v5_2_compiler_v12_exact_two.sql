BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_v5_local_reextract_maintainer') IS NULL
     OR to_regrole('memory_v5_local_supersession_maintainer') IS NULL
     OR to_regrole('memory_v5_2_local_router_maintainer') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regclass('memory.v5_local_packet_supersession') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL
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

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (reason_code IN (
    'temporal_persistence_matrix_reextracted',
    'pet_identity_semantics_reextracted',
    'duplicate_active_packet_reconciled',
    'semantic_compiler_v10_reextracted',
    'semantic_compiler_v11_reextracted',
    'semantic_compiler_v12_reextracted'
  ));

GRANT SELECT ON memory.v5_2_local_packet_route_event
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.evidence_intake_terminal
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.relational_stage_batch
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.observation
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.claim_observation
  TO memory_v5_local_supersession_maintainer;
GRANT SELECT ON memory.observation,memory.claim_observation,
  memory.v5_local_packet_supersession
  TO memory_v5_2_local_router_maintainer;

DROP POLICY IF EXISTS v5_2_compiler_v12_supersession_route_read
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY v5_2_compiler_v12_supersession_route_read
ON memory.v5_2_local_packet_route_event
FOR SELECT TO memory_v5_local_supersession_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_supersession_stage_read
  ON memory.relational_stage_batch;
CREATE POLICY v5_2_compiler_v12_supersession_stage_read
ON memory.relational_stage_batch
FOR SELECT TO memory_v5_local_supersession_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_supersession_observation_read
  ON memory.observation;
CREATE POLICY v5_2_compiler_v12_supersession_observation_read
ON memory.observation
FOR SELECT TO memory_v5_local_supersession_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_supersession_claim_link_read
  ON memory.claim_observation;
CREATE POLICY v5_2_compiler_v12_supersession_claim_link_read
ON memory.claim_observation
FOR SELECT TO memory_v5_local_supersession_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_route_observation_read
  ON memory.observation;
CREATE POLICY v5_2_compiler_v12_route_observation_read
ON memory.observation
FOR SELECT TO memory_v5_2_local_router_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_route_claim_link_read
  ON memory.claim_observation;
CREATE POLICY v5_2_compiler_v12_route_claim_link_read
ON memory.claim_observation
FOR SELECT TO memory_v5_2_local_router_maintainer
USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_2_compiler_v12_route_supersession_read
  ON memory.v5_local_packet_supersession;
CREATE POLICY v5_2_compiler_v12_route_supersession_read
ON memory.v5_local_packet_supersession
FOR SELECT TO memory_v5_2_local_router_maintainer
USING (owner_user_id=memory.current_actor_user_id());

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(
  p_prior_packet_id uuid,
  p_replacement_packet_id uuid
)
RETURNS TABLE(
  prior_packet_id uuid,
  replacement_packet_id uuid,
  evidence_id uuid,
  prior_packet_storage_sha256 text,
  replacement_packet_storage_sha256 text,
  prior_policy_compiler_sha256 text,
  replacement_policy_compiler_sha256 text,
  reason_code text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  selector constant text :=
    '20260731_v5_2_compiler_v12_exact_two_v1';
  manifest_sha constant text :=
    '13682f6959b491f1ab8c0e90cd54843c6164cc9b202b0683ccda53c430fe2052';
  compiler_sha constant text :=
    '91e3830e676b6c9abd881a52f3d6b010679809c86fffbc889134d9653deba5f3';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'compiler-v12 supersession plan requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL
     OR actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid THEN
    RAISE EXCEPTION 'compiler-v12 supersession actor is invalid'
      USING ERRCODE='42501';
  END IF;
  IF p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id=p_replacement_packet_id THEN
    RAISE EXCEPTION 'compiler-v12 supersession ids are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    prior.packet_id,
    replacement.packet_id,
    prior.evidence_id,
    prior.packet_storage_sha256,
    replacement.packet_storage_sha256,
    prior.policy_compiler_sha256,
    replacement.policy_compiler_sha256,
    'semantic_compiler_v12_reextracted'::text
  FROM memory.evidence_extraction_packet_v5_local AS prior
  JOIN memory.evidence_extraction_packet_v5_local AS replacement
    ON replacement.owner_user_id=prior.owner_user_id
   AND replacement.evidence_id=prior.evidence_id
   AND replacement.evidence_content_sha256=prior.evidence_content_sha256
  JOIN memory.evidence_extraction_job AS prior_job
    ON prior_job.owner_user_id=prior.owner_user_id
   AND prior_job.job_id=prior.job_id
  JOIN memory.evidence_extraction_job AS replacement_job
    ON replacement_job.owner_user_id=replacement.owner_user_id
   AND replacement_job.job_id=replacement.job_id
  JOIN memory.evidence_intake_terminal AS replacement_terminal
    ON replacement_terminal.owner_user_id=replacement_job.owner_user_id
   AND replacement_terminal.terminal_id=replacement_job.intake_terminal_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=prior.owner_user_id
   AND evidence.evidence_id=prior.evidence_id
  JOIN memory.v5_2_local_packet_route_event AS prior_route
    ON prior_route.owner_user_id=prior.owner_user_id
   AND prior_route.packet_id=prior.packet_id
  WHERE prior.owner_user_id=actor
    AND prior.packet_id=p_prior_packet_id
    AND replacement.packet_id=p_replacement_packet_id
    AND replacement.packet_id=
        memory.authoritative_owner_v5_2_packet_id_v1(prior.evidence_id)
    AND prior.created_at<replacement.created_at
    AND replacement.policy_compiler_sha256=compiler_sha
    AND replacement_job.selector_version=selector
    AND replacement_terminal.selector_version=selector
    AND replacement_terminal.details->>'manifest_sha256'=manifest_sha
    AND replacement_terminal.details->>'policy_compiler_sha256'=compiler_sha
    AND replacement_terminal.details->>'prior_packet_id'=prior.packet_id::text
    AND replacement_terminal.details->>'prior_packet_storage_sha256'=
        prior.packet_storage_sha256
    AND replacement_terminal.details->>'expected_route'=
        'manual_review_artifact_ready'
    AND prior_job.status='review_required'
    AND replacement_job.status='review_required'
    AND prior_job.route='relational_extraction'
    AND replacement_job.route='relational_extraction'
    AND replacement_job.lease_token IS NULL
    AND replacement_job.lease_expires_at IS NULL
    AND replacement_job.last_error IS NULL
    AND evidence.status='active'
    AND evidence.content_sha256=prior.evidence_content_sha256
    AND prior.provider_id='local_llama_cpp'
    AND replacement.provider_id='local_llama_cpp'
    AND replacement.local_model_calls BETWEEN 0 AND 1
    AND replacement.external_model_calls=0
    AND replacement.manual_review_required
    AND replacement.observation_count>0
    AND prior_route.route='manual_review_artifact_ready'
    AND prior.packet_storage_sha256=encode(public.digest(convert_to(
      prior.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND replacement.packet_storage_sha256=encode(public.digest(convert_to(
      replacement.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND (
      (
        prior.evidence_id=
          '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid
        AND prior.packet_id=
          '50405677-84aa-5d72-b800-89418fc606e4'::uuid
        AND prior.packet_storage_sha256=
          '6276f9c4cf03ee8276b96451e0c7915691ebfeb459036123a5f0fa977c826cbc'
        AND prior.policy_compiler_sha256=
          '738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6'
        AND prior_job.selector_version=
          '20260730_v5_2_semantic_compiler_v9_reextract_v1'
      ) OR (
        prior.evidence_id=
          '681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid
        AND prior.packet_id=
          'c4db1405-ad9d-5c9a-81e3-10ad0200b0ca'::uuid
        AND prior.packet_storage_sha256=
          '90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d'
        AND prior.policy_compiler_sha256=
          '275b5150f42e6d90e0afcebe86b32ab691be2e682c4d0c74e61947652230f6a2'
        AND prior_job.selector_version=
          '20260731_v5_2_semantic_compiler_v11_jerry_v1'
      )
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_supersession AS value
      WHERE value.owner_user_id=actor
        AND value.prior_packet_id=prior.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS value
      WHERE value.owner_user_id=actor
        AND value.packet_id=replacement.packet_id
    )
    AND (
      (
        prior.evidence_id=
          '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid
        AND EXISTS (
          SELECT 1 FROM memory.relational_stage_batch AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=prior.evidence_id
            AND value.batch_id=
              '7488403c-5fd9-47b3-a05a-ee4cad3b2561'::uuid
            AND value.extraction_packet_sha256=
              'c34bed1ba50cc612a343ea150c669e56a83f4bd47f4c04d3150a4c468743c82a'
            AND value.observation_count=1
        )
        AND 1=(
          SELECT count(*) FROM memory.observation AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=prior.evidence_id
            AND value.packet_sha256=
              'c34bed1ba50cc612a343ea150c669e56a83f4bd47f4c04d3150a4c468743c82a'
        )
      ) OR (
        prior.evidence_id=
          '681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid
        AND EXISTS (
          SELECT 1 FROM memory.relational_stage_batch AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=prior.evidence_id
            AND value.batch_id=
              '34f9af36-c895-443b-b84c-5c0656e22fa2'::uuid
            AND value.extraction_packet_sha256=
              '32f03737973cd7f2f4a95948e289eb5dbf5500bde388be20d4e9fe4b57184443'
            AND value.observation_count=2
        )
        AND 2=(
          SELECT count(*) FROM memory.observation AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=prior.evidence_id
            AND value.packet_sha256=
              '32f03737973cd7f2f4a95948e289eb5dbf5500bde388be20d4e9fe4b57184443'
        )
      )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.observation AS observation
      JOIN memory.claim_observation AS claim_link
        ON claim_link.owner_user_id=observation.owner_user_id
       AND claim_link.observation_id=observation.observation_id
      WHERE observation.owner_user_id=actor
        AND observation.evidence_id=prior.evidence_id
    );
END
$function$;

CREATE OR REPLACE FUNCTION
memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
  p_operation_id uuid,
  p_supersession_id uuid,
  p_prior_packet_id uuid,
  p_replacement_packet_id uuid,
  p_expected_prior_storage_sha256 text,
  p_expected_replacement_storage_sha256 text,
  p_reason_code text
)
RETURNS TABLE(
  supersession_id uuid,
  prior_packet_id uuid,
  replacement_packet_id uuid,
  reason_code text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  planned record;
  replayed memory.v5_local_packet_supersession%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'compiler-v12 supersession apply requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL
     OR actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid THEN
    RAISE EXCEPTION 'compiler-v12 supersession actor is invalid'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_supersession_id IS NULL
     OR p_prior_packet_id IS NULL OR p_replacement_packet_id IS NULL
     OR p_prior_packet_id=p_replacement_packet_id
     OR p_expected_prior_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_replacement_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_code<>'semantic_compiler_v12_reextracted' THEN
    RAISE EXCEPTION 'compiler-v12 supersession inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws(
    '|','memory_v1_v5_2_compiler_v12_supersession',actor::text,
    p_prior_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_local_packet_supersession AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id
      OR value.prior_packet_id=p_prior_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.supersession_id<>p_supersession_id
       OR replayed.prior_packet_id<>p_prior_packet_id
       OR replayed.replacement_packet_id<>p_replacement_packet_id
       OR replayed.prior_packet_storage_sha256<>
          p_expected_prior_storage_sha256
       OR replayed.replacement_packet_storage_sha256<>
          p_expected_replacement_storage_sha256
       OR replayed.reason_code<>p_reason_code THEN
      RAISE EXCEPTION 'compiler-v12 supersession replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.supersession_id,replayed.prior_packet_id,
      replayed.replacement_packet_id,replayed.reason_code,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(
    p_prior_packet_id,p_replacement_packet_id
  );
  IF NOT FOUND
     OR planned.prior_packet_storage_sha256<>
        p_expected_prior_storage_sha256
     OR planned.replacement_packet_storage_sha256<>
        p_expected_replacement_storage_sha256
     OR planned.reason_code<>p_reason_code THEN
    RAISE EXCEPTION 'compiler-v12 supersession plan changed'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_supersession(
    supersession_id,owner_user_id,operation_id,
    prior_packet_id,replacement_packet_id,evidence_id,reason_code,
    prior_packet_storage_sha256,replacement_packet_storage_sha256,
    prior_policy_compiler_sha256,replacement_policy_compiler_sha256
  ) VALUES (
    p_supersession_id,actor,p_operation_id,
    p_prior_packet_id,p_replacement_packet_id,planned.evidence_id,
    p_reason_code,planned.prior_packet_storage_sha256,
    planned.replacement_packet_storage_sha256,
    planned.prior_policy_compiler_sha256,
    planned.replacement_policy_compiler_sha256
  );
  RETURN QUERY SELECT p_supersession_id,p_prior_packet_id,
    p_replacement_packet_id,p_reason_code,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_v12_correction_route_v1(p_packet_id uuid)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  packet_storage_sha256 text,
  route text,
  reason_code text,
  routing_basis_sha256 text,
  source_deferral_reason_codes text[],
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  selector constant text :=
    '20260731_v5_2_compiler_v12_exact_two_v1';
  compiler_sha constant text :=
    '91e3830e676b6c9abd881a52f3d6b010679809c86fffbc889134d9653deba5f3';
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'compiler-v12 correction route requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL
     OR actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid THEN
    RAISE EXCEPTION 'compiler-v12 correction route actor is invalid'
      USING ERRCODE='42501';
  END IF;
  IF p_packet_id IS NULL THEN
    RAISE EXCEPTION 'compiler-v12 correction packet is required'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    packet.packet_id,
    packet.job_id,
    packet.evidence_id,
    packet.packet_storage_sha256,
    'manual_review_artifact_ready'::text,
    'reviewable_relational_packet_v5_2'::text,
    encode(public.digest(convert_to(jsonb_build_object(
      'packet_storage_sha256',packet.packet_storage_sha256,
      'route','manual_review_artifact_ready',
      'reason_codes','[]'::jsonb,
      'entity_mention_count',packet.entity_mention_count,
      'observation_count',packet.observation_count,
      'comparison_hint_count',packet.comparison_hint_count,
      'deferral_count',packet.deferral_count
    )::text,'UTF8'),'sha256'),'hex'),
    ARRAY[]::text[],
    packet.entity_mention_count,
    packet.observation_count,
    packet.comparison_hint_count,
    packet.deferral_count
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
   AND job.evidence_id=packet.evidence_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=packet.owner_user_id
   AND evidence.evidence_id=packet.evidence_id
  JOIN memory.v5_local_packet_supersession AS supersession
    ON supersession.owner_user_id=packet.owner_user_id
   AND supersession.replacement_packet_id=packet.packet_id
   AND supersession.evidence_id=packet.evidence_id
   AND supersession.reason_code='semantic_compiler_v12_reextracted'
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_packet_id
    AND packet.packet_id=
        memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id)
    AND packet.policy_compiler_sha256=compiler_sha
    AND job.selector_version=selector
    AND job.status='review_required'
    AND job.route='relational_extraction'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND job.last_error IS NULL
    AND evidence.status='active'
    AND evidence.content_sha256=packet.evidence_content_sha256
    AND packet.provider_id='local_llama_cpp'
    AND packet.local_model_calls BETWEEN 0 AND 1
    AND packet.external_model_calls=0
    AND packet.manual_review_required
    AND packet.entity_mention_count+packet.observation_count
          +packet.comparison_hint_count>=1
    AND packet.packet_storage_sha256=encode(public.digest(convert_to(
      packet.normalized_packet::text,'UTF8'
    ),'sha256'),'hex')
    AND (
      (
        packet.evidence_id=
          '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid
        AND supersession.prior_packet_id=
          '50405677-84aa-5d72-b800-89418fc606e4'::uuid
        AND supersession.prior_packet_storage_sha256=
          '6276f9c4cf03ee8276b96451e0c7915691ebfeb459036123a5f0fa977c826cbc'
        AND EXISTS (
          SELECT 1 FROM memory.relational_stage_batch AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=packet.evidence_id
            AND value.batch_id=
              '7488403c-5fd9-47b3-a05a-ee4cad3b2561'::uuid
            AND value.extraction_packet_sha256=
              'c34bed1ba50cc612a343ea150c669e56a83f4bd47f4c04d3150a4c468743c82a'
            AND value.observation_count=1
        )
        AND 1=(
          SELECT count(*) FROM memory.observation AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=packet.evidence_id
            AND value.packet_sha256=
              'c34bed1ba50cc612a343ea150c669e56a83f4bd47f4c04d3150a4c468743c82a'
        )
      ) OR (
        packet.evidence_id=
          '681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid
        AND supersession.prior_packet_id=
          'c4db1405-ad9d-5c9a-81e3-10ad0200b0ca'::uuid
        AND supersession.prior_packet_storage_sha256=
          '90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d'
        AND EXISTS (
          SELECT 1 FROM memory.relational_stage_batch AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=packet.evidence_id
            AND value.batch_id=
              '34f9af36-c895-443b-b84c-5c0656e22fa2'::uuid
            AND value.extraction_packet_sha256=
              '32f03737973cd7f2f4a95948e289eb5dbf5500bde388be20d4e9fe4b57184443'
            AND value.observation_count=2
        )
        AND 2=(
          SELECT count(*) FROM memory.observation AS value
          WHERE value.owner_user_id=actor
            AND value.evidence_id=packet.evidence_id
            AND value.packet_sha256=
              '32f03737973cd7f2f4a95948e289eb5dbf5500bde388be20d4e9fe4b57184443'
        )
      )
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.observation AS observation
      JOIN memory.claim_observation AS claim_link
        ON claim_link.owner_user_id=observation.owner_user_id
       AND claim_link.observation_id=observation.observation_id
      WHERE observation.owner_user_id=actor
        AND observation.evidence_id=packet.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS value
      WHERE value.owner_user_id=actor
        AND value.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS value
      WHERE value.owner_user_id=actor
        AND value.packet_id=packet.packet_id
    );
END
$function$;

CREATE OR REPLACE FUNCTION
memory.record_owner_v5_2_v12_correction_review_v1(
  p_operation_id uuid,
  p_route_event_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_routing_basis_sha256 text,
  p_review_id uuid,
  p_request_id uuid,
  p_review_report_sha256 text,
  p_stage_bundle_sha256 text,
  p_repository_commit text,
  p_auto_link_count integer,
  p_manual_review_count integer,
  p_deferred_resolution_count integer,
  p_rejected_count integer,
  p_blocking_code_count integer
)
RETURNS TABLE(
  route_event_id uuid,
  packet_id uuid,
  route text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  target record;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  replayed memory.v5_2_local_packet_route_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'compiler-v12 correction review requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL
     OR actor<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid THEN
    RAISE EXCEPTION 'compiler-v12 correction review actor is invalid'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_route_event_id IS NULL OR p_packet_id IS NULL
     OR p_review_id IS NULL OR p_request_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_routing_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_repository_commit !~ '^[0-9a-f]{40}$'
     OR p_auto_link_count NOT BETWEEN 0 AND 32
     OR p_manual_review_count NOT BETWEEN 0 AND 32
     OR p_deferred_resolution_count NOT BETWEEN 0 AND 32
     OR p_rejected_count NOT BETWEEN 0 AND 32
     OR p_blocking_code_count NOT BETWEEN 0 AND 32 THEN
    RAISE EXCEPTION 'compiler-v12 correction review inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_v12_correction_route',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_2_local_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'manual_review_artifact_ready'
       OR replayed.reason_code<>'reviewable_relational_packet_v5_2'
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.review_id<>p_review_id
       OR replayed.request_id<>p_request_id
       OR replayed.review_report_sha256<>p_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_stage_bundle_sha256
       OR replayed.repository_commit<>p_repository_commit
       OR replayed.auto_link_count<>p_auto_link_count
       OR replayed.manual_review_count<>p_manual_review_count
       OR replayed.deferred_resolution_count<>p_deferred_resolution_count
       OR replayed.rejected_count<>p_rejected_count
       OR replayed.blocking_code_count<>p_blocking_code_count THEN
      RAISE EXCEPTION 'compiler-v12 correction review replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.route_event_id,replayed.packet_id,
      replayed.route,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO target
  FROM memory.plan_owner_v5_2_v12_correction_route_v1(p_packet_id);
  IF NOT FOUND
     OR target.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR target.routing_basis_sha256<>p_expected_routing_basis_sha256 THEN
    RAISE EXCEPTION 'compiler-v12 correction review plan changed'
      USING ERRCODE='23514';
  END IF;

  SELECT value.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor
    AND value.packet_id=p_packet_id;
  IF p_auto_link_count+p_manual_review_count+p_deferred_resolution_count
       +p_rejected_count<>packet.entity_mention_count THEN
    RAISE EXCEPTION 'compiler-v12 review counts do not match packet'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_local_packet_route_event(
    route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    route,reason_code,routing_basis_sha256,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,entity_mention_count,
    observation_count,comparison_hint_count,deferral_count,
    source_deferral_reason_codes,review_id,request_id,review_contract,
    bundle_contract,review_report_sha256,stage_bundle_sha256,
    repository_commit,auto_link_count,manual_review_count,
    deferred_resolution_count,rejected_count,blocking_code_count
  ) VALUES (
    p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
    packet.evidence_id,'manual_review_artifact_ready',
    'reviewable_relational_packet_v5_2',p_expected_routing_basis_sha256,
    packet.evidence_content_sha256,packet.validator_packet_sha256,
    packet.packet_storage_sha256,packet.entity_mention_count,
    packet.observation_count,packet.comparison_hint_count,
    packet.deferral_count,ARRAY[]::text[],p_review_id,p_request_id,
    'memory_v1_v5_2_local_packet_review_v1',
    'memory_v1_v5_2_stage_preflight_v1',p_review_report_sha256,
    p_stage_bundle_sha256,p_repository_commit,p_auto_link_count,
    p_manual_review_count,p_deferred_resolution_count,p_rejected_count,
    p_blocking_code_count
  );

  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'manual_review_artifact_ready'::text,'applied'::text;
END
$function$;

ALTER FUNCTION
memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(uuid,uuid)
  OWNER TO memory_v5_local_supersession_maintainer;
ALTER FUNCTION
memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_local_supersession_maintainer;
REVOKE ALL ON FUNCTION
memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(uuid,uuid)
  FROM PUBLIC,brains_app,memory_v5_local_supersession_maintainer;
REVOKE ALL ON FUNCTION
memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
) FROM PUBLIC,brains_app,memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(uuid,uuid)
  TO brains_app,memory_v5_local_supersession_maintainer;
GRANT EXECUTE ON FUNCTION
memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text
) TO brains_app;

ALTER FUNCTION memory.plan_owner_v5_2_v12_correction_route_v1(uuid)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.record_owner_v5_2_v12_correction_review_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION
memory.plan_owner_v5_2_v12_correction_route_v1(uuid)
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_v12_correction_review_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION
memory.plan_owner_v5_2_v12_correction_route_v1(uuid)
  TO brains_app,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.record_owner_v5_2_v12_correction_review_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) TO brains_app;

COMMIT;
