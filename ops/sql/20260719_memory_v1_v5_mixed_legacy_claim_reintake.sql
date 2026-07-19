BEGIN;

DO $preflight$
BEGIN
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_intake_maintainer') IS NULL
     OR to_regrole('memory_extraction_queue_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.claim_evidence') IS NULL
     OR to_regclass('memory.predicate_contract') IS NULL
     OR to_regclass('memory.user_preference') IS NULL
     OR to_regclass('memory.preference_revision') IS NULL
     OR to_regclass('memory.preference_revision_evidence') IS NULL
     OR to_regclass('memory.project_knowledge_revision_evidence') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
  p_selector_version text,
  p_limit integer DEFAULT 100,
  p_evidence_id uuid DEFAULT NULL
)
RETURNS TABLE(
  evidence_id uuid,
  evidence_content_sha256 text,
  outcome text,
  route text,
  reason_code text,
  legacy_claim_count integer,
  active_preference_count integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_selector_version<>'20260719_v5_mixed_preference_legacy_reintake_v1'
     OR p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake plan inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    evidence.evidence_id,
    evidence.content_sha256,
    'eligible'::text,
    'relational_extraction'::text,
    'eligible_mixed_preference_relational_reintake'::text,
    claim_links.legacy_claim_count,
    preference_links.active_preference_count
  FROM memory.evidence AS evidence
  JOIN LATERAL (
    SELECT
      count(DISTINCT claim.claim_id) FILTER (
        WHERE contract.lifecycle='legacy_read_only'
      )::integer AS legacy_claim_count,
      count(DISTINCT claim.claim_id) FILTER (
        WHERE contract.predicate IS NULL
           OR contract.lifecycle<>'legacy_read_only'
      )::integer AS nonlegacy_claim_count
    FROM memory.claim_evidence AS link
    JOIN memory.claim AS claim
      ON claim.owner_user_id=link.owner_user_id
     AND claim.claim_id=link.claim_id
    LEFT JOIN memory.predicate_contract AS contract
      ON contract.registry_version='memory_predicate_registry_v5'
     AND contract.predicate=claim.predicate
    WHERE link.owner_user_id=actor
      AND link.evidence_id=evidence.evidence_id
  ) AS claim_links
    ON claim_links.legacy_claim_count>0
   AND claim_links.nonlegacy_claim_count=0
  JOIN LATERAL (
    SELECT count(DISTINCT head.preference_id)::integer
      AS active_preference_count
    FROM memory.preference_revision_evidence AS link
    JOIN memory.preference_revision AS revision
      ON revision.owner_user_id=link.owner_user_id
     AND revision.revision_id=link.revision_id
    JOIN memory.user_preference AS head
      ON head.owner_user_id=revision.owner_user_id
     AND head.preference_id=revision.preference_id
     AND head.current_revision_id=revision.revision_id
     AND head.status='active'
    WHERE link.owner_user_id=actor
      AND link.evidence_id=evidence.evidence_id
  ) AS preference_links
    ON preference_links.active_preference_count>0
  WHERE evidence.owner_user_id=actor
    AND evidence.status='active'
    AND evidence.kind IN ('user_statement','external_observation')
    AND evidence.content IS NOT NULL
    AND btrim(evidence.content)<>''
    AND evidence.content_sha256 ~ '^[0-9a-f]{64}$'
    AND (p_evidence_id IS NULL OR evidence.evidence_id=p_evidence_id)
    AND NOT EXISTS (
      SELECT 1 FROM memory.project_knowledge_revision_evidence AS link
      WHERE link.owner_user_id=actor
        AND link.evidence_id=evidence.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.evidence_id=evidence.evidence_id
        AND (
          job.selector_version=p_selector_version
          OR (
            job.route='relational_extraction'
            AND job.status::text IN (
              'pending','leased','review_required','completed'
            )
          )
        )
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id=actor
        AND stage.evidence_id=evidence.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.observation_entailment_v5 AS entailment
      WHERE entailment.owner_user_id=actor
        AND entailment.evidence_id=evidence.evidence_id
    )
  ORDER BY evidence.recorded_at,evidence.evidence_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(
  p_evidence_id uuid,
  p_selector_version text,
  p_expected_content_sha256 text,
  p_expected_route text,
  p_expected_reason_code text
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
  existing_job memory.evidence_extraction_job%ROWTYPE;
  existing_terminal memory.evidence_intake_terminal%ROWTYPE;
  job_found boolean;
  terminal_found boolean;
  new_job_id uuid := gen_random_uuid();
  new_terminal_id uuid := gen_random_uuid();
  fingerprint text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake enqueue requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_evidence_id IS NULL
     OR p_selector_version<>'20260719_v5_mixed_preference_legacy_reintake_v1'
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_route<>'relational_extraction'
     OR p_expected_reason_code<>'eligible_mixed_preference_relational_reintake' THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake enqueue inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','mixed_legacy_v5_reintake',actor::text,p_evidence_id::text,
      p_selector_version),0
  ));

  SELECT job.* INTO existing_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id=actor
    AND job.evidence_id=p_evidence_id
    AND job.selector_version=p_selector_version;
  job_found := FOUND;

  SELECT terminal.* INTO existing_terminal
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id=actor
    AND terminal.evidence_id=p_evidence_id
    AND terminal.selector_version=p_selector_version;
  terminal_found := FOUND;

  IF job_found OR terminal_found THEN
    IF NOT job_found OR NOT terminal_found
       OR existing_job.intake_terminal_id<>existing_terminal.terminal_id
       OR existing_job.evidence_content_sha256 IS DISTINCT FROM p_expected_content_sha256
       OR existing_terminal.evidence_content_sha256 IS DISTINCT FROM p_expected_content_sha256
       OR existing_job.route<>p_expected_route
       OR existing_job.intake_reason_code<>p_expected_reason_code
       OR existing_terminal.outcome<>'dispatched'
       OR existing_terminal.reason_code<>'eligible_dispatched' THEN
      RAISE EXCEPTION 'mixed legacy V5 reintake replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing_job.job_id,existing_terminal.terminal_id,
      existing_job.status::text,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
    p_selector_version,1,p_evidence_id
  );
  IF NOT FOUND OR planned.outcome<>'eligible'
     OR planned.evidence_content_sha256 IS DISTINCT FROM p_expected_content_sha256
     OR planned.route<>p_expected_route
     OR planned.reason_code<>p_expected_reason_code
     OR planned.legacy_claim_count<1
     OR planned.active_preference_count<1 THEN
    RAISE EXCEPTION 'mixed legacy V5 reintake plan changed'
      USING ERRCODE='23514';
  END IF;

  fingerprint := encode(public.digest(convert_to(jsonb_build_object(
    'owner_user_id',actor,
    'evidence_id',p_evidence_id,
    'selector_version',p_selector_version,
    'evidence_content_sha256',p_expected_content_sha256,
    'outcome','dispatched',
    'reason_code','eligible_dispatched',
    'route',p_expected_route,
    'migration','mixed_preference_legacy_claim_to_v5',
    'preference_lane','preserved'
  )::text,'UTF8'),'sha256'),'hex');

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,outcome,reason_code,
    evidence_content_sha256,source_job_id,source_job_status,
    source_job_pipeline_version,decision_fingerprint,actor_user_id,
    invoked_by_role,details
  ) VALUES (
    new_terminal_id,actor,p_evidence_id,p_selector_version,'dispatched',
    'eligible_dispatched',p_expected_content_sha256,NULL,NULL,NULL,fingerprint,
    actor,session_user,jsonb_build_object(
      'route',p_expected_route,'extraction_job_id',new_job_id,
      'plan_reason_code',p_expected_reason_code,
      'migration','mixed_preference_legacy_claim_to_v5',
      'preference_lane','preserved',
      'legacy_claim_count',planned.legacy_claim_count,
      'active_preference_count',planned.active_preference_count
    )
  );

  INSERT INTO memory.evidence_extraction_job(
    job_id,owner_user_id,evidence_id,intake_terminal_id,selector_version,
    evidence_content_sha256,route,intake_reason_code,status
  ) VALUES (
    new_job_id,actor,p_evidence_id,new_terminal_id,p_selector_version,
    p_expected_content_sha256,p_expected_route,p_expected_reason_code,'pending'
  );

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,job_id,event_type,from_status,to_status,actor_type,actor_ref,
    details
  ) VALUES (
    actor,new_job_id,'queued',NULL,'pending','system',
    'v5_mixed_legacy_claim_reintake',jsonb_build_object(
      'intake_terminal_id',new_terminal_id,
      'selector_version',p_selector_version,
      'route',p_expected_route,
      'migration','mixed_preference_legacy_claim_to_v5',
      'preference_lane','preserved'
    )
  );

  RETURN QUERY SELECT new_job_id,new_terminal_id,'pending'::text,'applied'::text;
END
$function$;

GRANT SELECT ON memory.evidence,memory.claim,memory.claim_evidence,
  memory.predicate_contract,memory.user_preference,memory.preference_revision,
  memory.preference_revision_evidence,memory.project_knowledge_revision_evidence,
  memory.evidence_extraction_job,memory.relational_stage_batch,
  memory.observation_entailment_v5
TO memory_intake_maintainer;

ALTER FUNCTION memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(
  text,integer,uuid
) OWNER TO memory_intake_maintainer;
ALTER FUNCTION memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(
  uuid,text,text,text,text
) OWNER TO memory_extraction_queue_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)
TO memory_extraction_queue_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)
FROM PUBLIC,brains_app,memory_intake_maintainer;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)
FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_mixed_legacy_claim_reintake_v1(text,integer,uuid)
TO brains_app,memory_intake_maintainer,memory_extraction_queue_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_v5_mixed_legacy_claim_reintake_v1(uuid,text,text,text,text)
TO brains_app;

COMMIT;
