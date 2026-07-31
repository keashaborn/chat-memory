BEGIN;

DO $$
BEGIN
  IF to_regrole('memory_context_rebind_maintainer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_evidence_intake_v2(text,integer,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_owner_contextual_split_v3(uuid,text,text,jsonb)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_owner_contextual_split_v3(uuid,text,text,jsonb,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_contextual_split_v3(uuid,text,text,text,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'contextual chat capture prerequisites are absent';
  END IF;
END
$$;

GRANT SELECT ON memory.evidence_contextual_span_v2,
  memory.evidence_intake_terminal
  TO memory_context_rebind_maintainer;

CREATE OR REPLACE FUNCTION memory.plan_owner_contextual_chat_capture_v1(
  p_selector_version text,
  p_limit integer DEFAULT 100
)
RETURNS TABLE(
  evidence_id uuid,
  evidence_content_sha256 text,
  outcome text,
  route text,
  reason_code text,
  source_job_id uuid,
  source_job_status text,
  source_job_pipeline_version text,
  upstream_candidate_count integer,
  source_bound boolean,
  requires_contextual_split boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION
      'contextual chat capture plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_selector_version<>'20260729_v4_contextual_resplit'
     OR p_limit IS NULL OR p_limit<1 OR p_limit>500 THEN
    RAISE EXCEPTION 'contextual chat capture plan inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  SELECT
    evidence.evidence_id,
    evidence.content_sha256,
    'deferred'::text,
    'contextual_split'::text,
    'contextual_split_required'::text,
    NULL::uuid,
    NULL::text,
    NULL::text,
    0,
    (
      source.id IS NOT NULL
      AND source.text IS NOT DISTINCT FROM evidence.content
      AND source.thread_id IS NOT NULL
      AND source.request_id
        ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND evidence.metadata->>'source_content_sha256'
        IS NOT DISTINCT FROM evidence.content_sha256
      AND encode(
        public.digest(convert_to(source.text,'UTF8'),'sha256'),'hex'
      ) IS NOT DISTINCT FROM evidence.content_sha256
      AND evidence.metadata->>'thread_id'
        IS NOT DISTINCT FROM source.thread_id::text
      AND evidence.metadata->>'request_id'
        IS NOT DISTINCT FROM source.request_id
      AND evidence.metadata->>'source_char_start'='0'
      AND evidence.metadata->>'source_char_end'
        =length(source.text)::text
      AND evidence.metadata->>'primary_lane'
        ='unclassified_user_statement'
      AND evidence.metadata->>'epistemic_role'
        ='user_report_unclassified'
      AND evidence.metadata->>'span_origin'='raw_chat_turn_v1'
    ) AS source_bound,
    true
  FROM memory.evidence AS evidence
  LEFT JOIN public.chat_log AS source
    ON source.owner_user_id=actor
   AND source.id=CASE
     WHEN evidence.metadata->>'source_id'
       ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     THEN (evidence.metadata->>'source_id')::uuid
     ELSE NULL
   END
   AND source.source='frontend/chat:user'
  WHERE evidence.owner_user_id=actor
    AND evidence.status='active'
    AND evidence.kind='user_statement'
    AND evidence.source_system='public.chat_log'
    AND evidence.metadata->>'capture_version'
      ='memory_v1_v5_chat_capture_20260730_v2_contextual'
    AND evidence.metadata->>'source_type'='frontend/chat:user'
    AND evidence.metadata->>'semantic_processing'='pending'
    AND NOT (evidence.metadata ? 'contextual_parent_evidence_id')
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_contextual_span_v2 AS span
      WHERE span.owner_user_id=actor
        AND span.parent_evidence_id=evidence.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id=actor
        AND terminal.evidence_id=evidence.evidence_id
        AND terminal.selector_version=p_selector_version
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id=actor
        AND job.evidence_id=evidence.evidence_id
    )
  ORDER BY evidence.recorded_at,evidence.evidence_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.plan_owner_contextual_chat_capture_v1(text,integer)
  OWNER TO memory_context_rebind_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_contextual_chat_capture_v1(text,integer)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_contextual_chat_capture_v1(text,integer)
  TO brains_app;

DO $$
DECLARE
  function_oid oid:=
    'memory.plan_owner_contextual_chat_capture_v1(text,integer)'::regprocedure;
BEGIN
  IF (
    SELECT proowner<>to_regrole('memory_context_rebind_maintainer')
      OR prosecdef IS DISTINCT FROM true
      OR provolatile<>'s'
      OR proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog']
    FROM pg_proc
    WHERE oid=function_oid
  ) OR NOT has_function_privilege(
    'brains_app',function_oid,'EXECUTE'
  ) OR EXISTS (
    SELECT 1
    FROM pg_proc AS function_row
    CROSS JOIN LATERAL aclexplode(
      coalesce(
        function_row.proacl,
        acldefault('f',function_row.proowner)
      )
    ) AS privilege
    WHERE function_row.oid=function_oid
      AND privilege.grantee=0
      AND privilege.privilege_type='EXECUTE'
  ) THEN
    RAISE EXCEPTION 'contextual chat capture function ACL is invalid';
  END IF;
  IF NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.evidence'::regclass
  ) OR NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.evidence_extraction_job'::regclass
  ) OR NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.evidence_intake_terminal'::regclass
  ) THEN
    RAISE EXCEPTION 'contextual chat capture forced RLS is absent';
  END IF;
  IF NOT has_table_privilege(
    'memory_context_rebind_maintainer',
    'memory.evidence_contextual_span_v2','SELECT'
  ) OR NOT has_table_privilege(
    'memory_context_rebind_maintainer',
    'memory.evidence_intake_terminal','SELECT'
  ) THEN
    RAISE EXCEPTION 'contextual chat capture read ACL is absent';
  END IF;
END
$$;

COMMIT;
