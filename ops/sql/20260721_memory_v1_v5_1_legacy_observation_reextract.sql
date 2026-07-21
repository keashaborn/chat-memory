BEGIN;

DO $preflight$
BEGIN
  IF to_regrole('brains_app') IS NULL
     OR to_regrole('memory_intake_maintainer') IS NULL
     OR to_regrole('memory_extraction_queue_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence_intake_terminal') IS NULL
     OR to_regclass('memory.evidence_extraction_event') IS NULL THEN
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction prerequisites are absent';
  END IF;
END
$preflight$;

DROP POLICY IF EXISTS v5_1_legacy_observation_reextract_read
  ON memory.observation;
CREATE POLICY v5_1_legacy_observation_reextract_read
  ON memory.observation
  FOR SELECT
  TO memory_intake_maintainer
  USING (owner_user_id=memory.current_actor_user_id());

DROP POLICY IF EXISTS v5_1_legacy_packet_reextract_read
  ON memory.evidence_extraction_packet_v5_local;
CREATE POLICY v5_1_legacy_packet_reextract_read
  ON memory.evidence_extraction_packet_v5_local
  FOR SELECT
  TO memory_intake_maintainer
  USING (owner_user_id=memory.current_actor_user_id());

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_1_legacy_observation_reextract_v1(
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
  legacy_observation_count integer
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
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_selector_version<>'20260721_v5_1_legacy_observation_reextract_v1'
     OR p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction plan inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    evidence.evidence_id,
    evidence.content_sha256,
    'eligible'::text,
    'relational_extraction'::text,
    'eligible_unprocessed'::text,
    count(observation.observation_id)::integer
  FROM memory.evidence AS evidence
  JOIN memory.observation AS observation
    ON observation.owner_user_id=evidence.owner_user_id
   AND observation.evidence_id=evidence.evidence_id
   AND observation.predicate_registry_version='memory_predicate_registry_v5'
  WHERE evidence.owner_user_id=actor
    AND evidence.status='active'
    AND evidence.kind IN ('user_statement','external_observation')
    AND evidence.content IS NOT NULL
    AND btrim(evidence.content)<>''
    AND evidence.content_sha256 ~ '^[0-9a-f]{64}$'
    AND (p_evidence_id IS NULL OR evidence.evidence_id=p_evidence_id)
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.evidence_id=evidence.evidence_id
        AND job.selector_version=p_selector_version
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id=actor
        AND packet.evidence_id=evidence.evidence_id
        AND packet.normalized_packet->>'contract_version'=
          'memory_v1_relational_extraction_v5_1'
        AND packet.normalized_packet->>'predicate_registry_version'=
          'memory_predicate_registry_v5_1'
    )
  GROUP BY evidence.evidence_id,evidence.content_sha256,evidence.recorded_at
  ORDER BY evidence.recorded_at,evidence.evidence_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
  p_evidence_id uuid,
  p_selector_version text,
  p_expected_content_sha256 text,
  p_expected_route text,
  p_expected_reason_code text,
  p_expected_legacy_observation_count integer
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
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction enqueue requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_evidence_id IS NULL
     OR p_selector_version<>'20260721_v5_1_legacy_observation_reextract_v1'
     OR p_expected_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_route<>'relational_extraction'
     OR p_expected_reason_code<>'eligible_unprocessed'
     OR p_expected_legacy_observation_count IS NULL
     OR p_expected_legacy_observation_count<1 THEN
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction enqueue inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','v5_1_legacy_observation_reextract',actor::text,
      p_evidence_id::text,p_selector_version),0
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
       OR existing_terminal.reason_code<>'eligible_dispatched'
       OR existing_terminal.details->>'migration'<>'v5_observation_to_v5_1' THEN
      RAISE EXCEPTION 'V5.1 legacy observation re-extraction replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing_job.job_id,existing_terminal.terminal_id,
      existing_job.status::text,'replayed'::text;
    RETURN;
  END IF;

  SELECT * INTO planned
  FROM memory.plan_owner_v5_1_legacy_observation_reextract_v1(
    p_selector_version,1,p_evidence_id
  );
  IF NOT FOUND OR planned.outcome<>'eligible'
     OR planned.evidence_content_sha256 IS DISTINCT FROM p_expected_content_sha256
     OR planned.route<>p_expected_route
     OR planned.reason_code<>p_expected_reason_code
     OR planned.legacy_observation_count<>p_expected_legacy_observation_count THEN
    RAISE EXCEPTION 'V5.1 legacy observation re-extraction plan changed'
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
    'legacy_observation_count',p_expected_legacy_observation_count,
    'migration','v5_observation_to_v5_1'
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
      'route',p_expected_route,
      'extraction_job_id',new_job_id,
      'plan_reason_code',p_expected_reason_code,
      'legacy_observation_count',p_expected_legacy_observation_count,
      'migration','v5_observation_to_v5_1'
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
    'v5_1_legacy_observation_reextract',jsonb_build_object(
      'intake_terminal_id',new_terminal_id,
      'selector_version',p_selector_version,
      'route',p_expected_route,
      'migration','v5_observation_to_v5_1'
    )
  );

  RETURN QUERY SELECT new_job_id,new_terminal_id,'pending'::text,'applied'::text;
END
$function$;

GRANT SELECT ON memory.evidence,memory.observation,
  memory.evidence_extraction_packet_v5_local,memory.evidence_extraction_job
TO memory_intake_maintainer;

ALTER FUNCTION memory.plan_owner_v5_1_legacy_observation_reextract_v1(
  text,integer,uuid
) OWNER TO memory_intake_maintainer;
ALTER FUNCTION memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
  uuid,text,text,text,text,integer
) OWNER TO memory_extraction_queue_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_1_legacy_observation_reextract_v1(text,integer,uuid)
TO memory_extraction_queue_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_1_legacy_observation_reextract_v1(text,integer,uuid)
FROM PUBLIC,brains_app,memory_intake_maintainer;
REVOKE ALL ON FUNCTION
  memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
    uuid,text,text,text,text,integer
  )
FROM PUBLIC,brains_app,memory_extraction_queue_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_1_legacy_observation_reextract_v1(text,integer,uuid)
TO brains_app,memory_intake_maintainer,memory_extraction_queue_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
    uuid,text,text,text,text,integer
  )
TO brains_app;

COMMIT;
