BEGIN;

DO $$
BEGIN
  IF to_regclass('memory.evidence_contextual_span_v2') IS NULL
     OR to_regprocedure(
       'memory.preflight_owner_contextual_split_v2(uuid,text,text,jsonb)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_owner_contextual_split_v2(uuid,text,text,jsonb,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_contextual_split_v2(uuid,text,text,text,integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'contextual split v2 prerequisite is absent';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM memory.evidence_contextual_span_v2
     WHERE splitter_version <>
       'memory_v1_contextual_span_splitter_20260728_v2'
        OR span_origin <> 'contextual_split_v2'
  ) THEN
    RAISE EXCEPTION 'unexpected contextual splitter state';
  END IF;
END
$$;

LOCK TABLE memory.evidence_contextual_span_v2
  IN ACCESS EXCLUSIVE MODE;

ALTER TABLE memory.evidence_contextual_span_v2
  DROP CONSTRAINT evidence_contextual_span_v2_boundary_reason_check,
  DROP CONSTRAINT evidence_contextual_span_v2_span_origin_check,
  DROP CONSTRAINT evidence_contextual_span_v2_splitter_version_check;

ALTER TABLE memory.evidence_contextual_span_v2
  ADD CONSTRAINT evidence_contextual_span_v2_boundary_reason_check
    CHECK (boundary_reason IN (
      'sentence_boundary','clause_boundary','length_boundary',
      'terminal_span','appositive_name_list_coalesced'
    )),
  ADD CONSTRAINT evidence_contextual_span_v2_span_origin_check
    CHECK (span_origin IN ('contextual_split_v2','contextual_split_v3')),
  ADD CONSTRAINT evidence_contextual_span_v2_splitter_version_check
    CHECK (splitter_version IN (
      'memory_v1_contextual_span_splitter_20260728_v2',
      'memory_v1_contextual_span_splitter_20260728_v3'
    ));

CREATE OR REPLACE FUNCTION memory.preflight_owner_contextual_split_v3(
  p_parent_evidence_id uuid,
  p_expected_parent_content_sha256 text,
  p_splitter_version text,
  p_spans jsonb
)
RETURNS TABLE(
  plan_sha256 text,
  span_count integer,
  span_plan jsonb,
  source_id uuid,
  thread_id uuid,
  request_id uuid,
  source_content_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  parent memory.evidence%ROWTYPE;
  source public.chat_log%ROWTYPE;
  item jsonb;
  plan jsonb := '[]'::jsonb;
  ordinal_value integer := 0;
  start_value integer;
  end_value integer;
  previous_end integer := 0;
  content_value text;
  content_hash text;
  child_id uuid;
  span_id_value uuid;
  external_id_value text;
  wrapper jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'contextual split preflight requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_parent_evidence_id IS NULL
     OR p_expected_parent_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_splitter_version
          <>'memory_v1_contextual_span_splitter_20260728_v3'
     OR jsonb_typeof(p_spans)<>'array'
     OR jsonb_array_length(p_spans)<1
     OR jsonb_array_length(p_spans)>64 THEN
    RAISE EXCEPTION 'contextual split preflight inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT evidence.* INTO parent
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_parent_evidence_id;
  IF NOT FOUND OR parent.status<>'active'
     OR parent.kind<>'user_statement'
     OR parent.source_system<>'public.chat_log'
     OR parent.content IS NULL OR btrim(parent.content)=''
     OR parent.content_sha256 IS DISTINCT FROM p_expected_parent_content_sha256
     OR parent.metadata ? 'contextual_parent_evidence_id' THEN
    RAISE EXCEPTION 'owner contextual split parent is unavailable'
      USING ERRCODE='P0002';
  END IF;

  SELECT log.* INTO source
  FROM public.chat_log AS log
  WHERE log.owner_user_id=actor
    AND log.source IN (
      'frontend/chat:user',
      'backend/web:user',
      'voice/realtime-preview:user'
    )
    AND log.id=CASE
      WHEN parent.metadata->>'source_id'
        ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      THEN (parent.metadata->>'source_id')::uuid
      WHEN parent.external_id
        ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      THEN parent.external_id::uuid
      WHEN parent.external_id
        ~ '^chat_log:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      THEN substring(parent.external_id FROM 10)::uuid
      ELSE NULL
    END;
  IF NOT FOUND OR source.text IS DISTINCT FROM parent.content
     OR source.thread_id IS NULL OR source.request_id IS NULL
     OR source.request_id
          !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     OR encode(
          public.digest(convert_to(source.text,'UTF8'),'sha256'),'hex'
        ) IS DISTINCT FROM parent.content_sha256 THEN
    RAISE EXCEPTION 'contextual split raw source binding is invalid'
      USING ERRCODE='23514';
  END IF;

  FOR item IN
    SELECT value
    FROM jsonb_array_elements(p_spans) AS rows(value)
    ORDER BY (value->>'ordinal')::integer
  LOOP
    IF NOT memory.v5_jsonb_exact_keys(item,ARRAY[
      'ordinal','char_start','char_end','content','content_sha256',
      'boundary_reason','context_needed','primary_lane','epistemic_role',
      'span_origin'
    ])
       OR jsonb_typeof(item->'ordinal')<>'number'
       OR jsonb_typeof(item->'char_start')<>'number'
       OR jsonb_typeof(item->'char_end')<>'number'
       OR jsonb_typeof(item->'content')<>'string'
       OR jsonb_typeof(item->'content_sha256')<>'string'
       OR jsonb_typeof(item->'boundary_reason')<>'string'
       OR jsonb_typeof(item->'context_needed')<>'boolean'
       OR jsonb_typeof(item->'primary_lane')<>'string'
       OR jsonb_typeof(item->'epistemic_role')<>'string'
       OR jsonb_typeof(item->'span_origin')<>'string' THEN
      RAISE EXCEPTION 'contextual split span shape is invalid'
        USING ERRCODE='22023';
    END IF;
    IF (item->>'ordinal')::integer<>ordinal_value THEN
      RAISE EXCEPTION 'contextual split ordinals must be contiguous'
        USING ERRCODE='23514';
    END IF;
    start_value := (item->>'char_start')::integer;
    end_value := (item->>'char_end')::integer;
    content_value := item->>'content';
    content_hash := item->>'content_sha256';
    IF start_value<previous_end OR end_value<=start_value
       OR end_value>length(parent.content)
       OR length(content_value)>600 OR btrim(content_value)=''
       OR item->>'boundary_reason' NOT IN (
         'sentence_boundary','clause_boundary','length_boundary','terminal_span','appositive_name_list_coalesced'
       )
       OR item->>'primary_lane'<>'unclassified_user_statement'
       OR item->>'epistemic_role'<>'user_report_unclassified'
       OR item->>'span_origin'<>'contextual_split_v3'
       OR content_hash !~ '^[0-9a-f]{64}$'
       OR substring(
            parent.content FROM start_value+1 FOR end_value-start_value
          ) IS DISTINCT FROM content_value
       OR encode(
            public.digest(convert_to(content_value,'UTF8'),'sha256'),'hex'
          ) IS DISTINCT FROM content_hash
       OR regexp_replace(
            substring(
              parent.content FROM previous_end+1 FOR start_value-previous_end
            ),
            '[[:space:]]','','g'
          )<>'' THEN
      RAISE EXCEPTION 'contextual split content or coverage is invalid'
        USING ERRCODE='23514';
    END IF;
    child_id := memory.atomic_span_uuid_v1(concat_ws('|',
      'contextual-child-v3',actor::text,parent.evidence_id::text,
      parent.content_sha256,p_splitter_version,ordinal_value::text,
      start_value::text,end_value::text,content_hash
    ));
    span_id_value := memory.atomic_span_uuid_v1(concat_ws('|',
      'contextual-span-v3',actor::text,parent.evidence_id::text,
      parent.content_sha256,p_splitter_version,ordinal_value::text,
      start_value::text,end_value::text,content_hash
    ));
    external_id_value := concat_ws(
      ':','contextual-v3',source.id::text,lpad(ordinal_value::text,2,'0'),
      substr(content_hash,1,16)
    );
    plan := plan || jsonb_build_array(jsonb_build_object(
      'ordinal',ordinal_value,
      'span_id',span_id_value,
      'child_evidence_id',child_id,
      'external_id',external_id_value,
      'char_start',start_value,
      'char_end',end_value,
      'content',content_value,
      'content_sha256',content_hash,
      'boundary_reason',item->>'boundary_reason',
      'context_needed',(item->>'context_needed')::boolean,
      'primary_lane',item->>'primary_lane',
      'epistemic_role',item->>'epistemic_role',
      'span_origin',item->>'span_origin'
    ));
    previous_end := end_value;
    ordinal_value := ordinal_value+1;
  END LOOP;
  IF ordinal_value<>jsonb_array_length(p_spans)
     OR regexp_replace(
          substring(parent.content FROM previous_end+1),
          '[[:space:]]','','g'
        )<>'' THEN
    RAISE EXCEPTION 'contextual split source coverage is incomplete'
      USING ERRCODE='23514';
  END IF;

  wrapper := jsonb_build_object(
    'contract_version','memory_v1_contextual_split_plan_v3',
    'owner_user_id',actor,
    'parent_evidence_id',parent.evidence_id,
    'parent_content_sha256',parent.content_sha256,
    'source_id',source.id,
    'source_content_sha256',parent.content_sha256,
    'thread_id',source.thread_id,
    'request_id',source.request_id,
    'splitter_version',p_splitter_version,
    'spans',plan
  );
  RETURN QUERY SELECT
    encode(public.digest(convert_to(wrapper::text,'UTF8'),'sha256'),'hex'),
    ordinal_value,
    plan,
    source.id,
    source.thread_id,
    source.request_id::uuid,
    parent.content_sha256;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_owner_contextual_split_v3(
  p_parent_evidence_id uuid,
  p_expected_parent_content_sha256 text,
  p_splitter_version text,
  p_spans jsonb,
  p_expected_plan_sha256 text
)
RETURNS TABLE(
  child_evidence_id uuid,
  ordinal integer,
  child_content_sha256 text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  parent memory.evidence%ROWTYPE;
  planned record;
  item jsonb;
  expected_metadata jsonb;
  existing_evidence memory.evidence%ROWTYPE;
  existing_span memory.evidence_contextual_span_v2%ROWTYPE;
  row_outcome text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'contextual split apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_expected_plan_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'contextual split apply inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'contextual_split_v3',actor::text,p_parent_evidence_id::text,
    p_splitter_version
  ),0));
  SELECT * INTO planned
  FROM memory.preflight_owner_contextual_split_v3(
    p_parent_evidence_id,p_expected_parent_content_sha256,
    p_splitter_version,p_spans
  );
  IF planned.plan_sha256 IS DISTINCT FROM p_expected_plan_sha256 THEN
    RAISE EXCEPTION 'contextual split plan SHA-256 changed'
      USING ERRCODE='23514';
  END IF;
  SELECT evidence.* INTO STRICT parent
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_parent_evidence_id;

  FOR item IN
    SELECT value
    FROM jsonb_array_elements(planned.span_plan) AS rows(value)
    ORDER BY (value->>'ordinal')::integer
  LOOP
    row_outcome := 'applied';
    expected_metadata := coalesce(parent.metadata,'{}'::jsonb)
      || jsonb_build_object(
        'source_id',planned.source_id,
        'source_external_id',planned.source_id,
        'source_content_sha256',planned.source_content_sha256,
        'thread_id',planned.thread_id,
        'request_id',planned.request_id,
        'source_char_start',(item->>'char_start')::integer,
        'source_char_end',(item->>'char_end')::integer,
        'primary_lane',item->>'primary_lane',
        'epistemic_role',item->>'epistemic_role',
        'span_origin',item->>'span_origin',
        'context_needed',(item->>'context_needed')::boolean,
        'contextual_parent_evidence_id',parent.evidence_id,
        'contextual_parent_content_sha256',parent.content_sha256,
        'contextual_splitter_version',p_splitter_version,
        'contextual_span_id',(item->>'span_id')::uuid,
        'contextual_ordinal',(item->>'ordinal')::integer,
        'contextual_boundary_reason',item->>'boundary_reason',
        'contextual_plan_sha256',planned.plan_sha256,
        'semantic_processing','pending'
      );

    SELECT evidence.* INTO existing_evidence
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND (
        evidence.evidence_id=(item->>'child_evidence_id')::uuid
        OR (
          evidence.source_system=parent.source_system
          AND evidence.external_id=item->>'external_id'
        )
      );
    IF FOUND THEN
      IF existing_evidence.evidence_id<>(item->>'child_evidence_id')::uuid
         OR existing_evidence.kind<>parent.kind
         OR existing_evidence.source_system<>parent.source_system
         OR existing_evidence.external_id<>(item->>'external_id')
         OR existing_evidence.content IS DISTINCT FROM item->>'content'
         OR existing_evidence.content_sha256
              IS DISTINCT FROM item->>'content_sha256'
         OR existing_evidence.observed_at IS DISTINCT FROM parent.observed_at
         OR existing_evidence.recorded_at IS DISTINCT FROM parent.recorded_at
         OR existing_evidence.directness IS DISTINCT FROM parent.directness
         OR existing_evidence.source_reliability
              IS DISTINCT FROM parent.source_reliability
         OR existing_evidence.independence_key
              IS DISTINCT FROM parent.independence_key
         OR existing_evidence.sensitivity IS DISTINCT FROM parent.sensitivity
         OR existing_evidence.status<>'active'
         OR existing_evidence.metadata IS DISTINCT FROM expected_metadata THEN
        RAISE EXCEPTION 'contextual child evidence replay conflicts'
          USING ERRCODE='23514';
      END IF;
      row_outcome := 'replayed';
    ELSE
      INSERT INTO memory.evidence(
        evidence_id,owner_user_id,kind,source_system,external_id,content,
        content_sha256,observed_at,recorded_at,directness,source_reliability,
        independence_key,sensitivity,status,metadata
      ) VALUES (
        (item->>'child_evidence_id')::uuid,actor,parent.kind,
        parent.source_system,item->>'external_id',item->>'content',
        item->>'content_sha256',parent.observed_at,parent.recorded_at,
        parent.directness,parent.source_reliability,parent.independence_key,
        parent.sensitivity,'active',expected_metadata
      );
    END IF;

    SELECT span.* INTO existing_span
    FROM memory.evidence_contextual_span_v2 AS span
    WHERE span.owner_user_id=actor
      AND span.span_id=(item->>'span_id')::uuid;
    IF FOUND THEN
      IF existing_span.parent_evidence_id<>parent.evidence_id
         OR existing_span.child_evidence_id
              <>(item->>'child_evidence_id')::uuid
         OR existing_span.source_id<>planned.source_id
         OR existing_span.thread_id<>planned.thread_id
         OR existing_span.request_id<>planned.request_id
         OR existing_span.splitter_version<>p_splitter_version
         OR existing_span.parent_content_sha256<>parent.content_sha256
         OR existing_span.child_content_sha256<>item->>'content_sha256'
         OR existing_span.source_content_sha256
              <>planned.source_content_sha256
         OR existing_span.ordinal<>(item->>'ordinal')::integer
         OR existing_span.char_start<>(item->>'char_start')::integer
         OR existing_span.char_end<>(item->>'char_end')::integer
         OR existing_span.boundary_reason<>item->>'boundary_reason'
         OR existing_span.context_needed
              IS DISTINCT FROM (item->>'context_needed')::boolean
         OR existing_span.primary_lane<>item->>'primary_lane'
         OR existing_span.epistemic_role<>item->>'epistemic_role'
         OR existing_span.span_origin<>item->>'span_origin'
         OR existing_span.plan_sha256<>planned.plan_sha256 THEN
        RAISE EXCEPTION 'contextual span replay conflicts'
          USING ERRCODE='23514';
      END IF;
    ELSE
      INSERT INTO memory.evidence_contextual_span_v2(
        span_id,owner_user_id,parent_evidence_id,child_evidence_id,
        source_id,thread_id,request_id,splitter_version,
        parent_content_sha256,child_content_sha256,source_content_sha256,
        ordinal,char_start,char_end,boundary_reason,context_needed,
        primary_lane,epistemic_role,span_origin,plan_sha256
      ) VALUES (
        (item->>'span_id')::uuid,actor,parent.evidence_id,
        (item->>'child_evidence_id')::uuid,planned.source_id,
        planned.thread_id,planned.request_id,p_splitter_version,
        parent.content_sha256,item->>'content_sha256',
        planned.source_content_sha256,
        (item->>'ordinal')::integer,(item->>'char_start')::integer,
        (item->>'char_end')::integer,item->>'boundary_reason',
        (item->>'context_needed')::boolean,item->>'primary_lane',
        item->>'epistemic_role',item->>'span_origin',planned.plan_sha256
      );
    END IF;
    RETURN QUERY SELECT
      (item->>'child_evidence_id')::uuid,
      (item->>'ordinal')::integer,
      item->>'content_sha256',
      row_outcome;
  END LOOP;
END
$function$;

CREATE OR REPLACE FUNCTION memory.finalize_owner_contextual_split_v3(
  p_parent_evidence_id uuid,
  p_selector_version text,
  p_expected_parent_content_sha256 text,
  p_expected_plan_sha256 text,
  p_expected_child_count integer
)
RETURNS TABLE(
  terminal_id uuid,
  outcome text,
  apply_outcome text,
  decision_fingerprint text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  existing memory.evidence_intake_terminal%ROWTYPE;
  fingerprint text;
  inserted memory.evidence_intake_terminal%ROWTYPE;
  actual_count integer;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'contextual split finalizer requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_parent_evidence_id IS NULL
     OR p_selector_version
          <>'20260729_v4_contextual_resplit'
     OR p_expected_parent_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_plan_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_child_count<1 OR p_expected_child_count>64 THEN
    RAISE EXCEPTION 'contextual split finalizer inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'contextual_split_finalizer_v2',actor::text,p_parent_evidence_id::text,
    p_selector_version
  ),0));

  SELECT count(*)::integer INTO actual_count
  FROM memory.evidence_contextual_span_v2 AS span
  JOIN memory.evidence AS child
    ON child.owner_user_id=span.owner_user_id
   AND child.evidence_id=span.child_evidence_id
  WHERE span.owner_user_id=actor
    AND span.parent_evidence_id=p_parent_evidence_id
    AND span.parent_content_sha256=p_expected_parent_content_sha256
    AND span.plan_sha256=p_expected_plan_sha256
    AND child.status='active'
    AND child.content_sha256=span.child_content_sha256;
  IF actual_count IS DISTINCT FROM p_expected_child_count THEN
    RAISE EXCEPTION 'contextual split children are incomplete'
      USING ERRCODE='23514';
  END IF;

  fingerprint := encode(public.digest(convert_to(jsonb_build_object(
    'owner_user_id',actor,
    'parent_evidence_id',p_parent_evidence_id,
    'selector_version',p_selector_version,
    'parent_content_sha256',p_expected_parent_content_sha256,
    'plan_sha256',p_expected_plan_sha256,
    'child_count',p_expected_child_count,
    'outcome','skipped',
    'reason_code','contextual_split_parent'
  )::text,'UTF8'),'sha256'),'hex');

  SELECT terminal.* INTO existing
  FROM memory.evidence_intake_terminal AS terminal
  WHERE terminal.owner_user_id=actor
    AND terminal.evidence_id=p_parent_evidence_id
    AND terminal.selector_version=p_selector_version;
  IF FOUND THEN
    IF existing.outcome<>'skipped'
       OR existing.reason_code<>'contextual_split_parent'
       OR existing.evidence_content_sha256
            IS DISTINCT FROM p_expected_parent_content_sha256
       OR existing.decision_fingerprint<>fingerprint
       OR existing.details IS DISTINCT FROM jsonb_build_object(
         'route','contextual_split',
         'splitter_version',
           'memory_v1_contextual_span_splitter_20260728_v3',
         'plan_sha256',p_expected_plan_sha256,
         'child_count',p_expected_child_count
       ) THEN
      RAISE EXCEPTION 'contextual split finalizer replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.terminal_id,existing.outcome,'replayed'::text,
      existing.decision_fingerprint;
    RETURN;
  END IF;

  INSERT INTO memory.evidence_intake_terminal(
    terminal_id,owner_user_id,evidence_id,selector_version,outcome,
    reason_code,evidence_content_sha256,source_job_id,source_job_status,
    source_job_pipeline_version,decision_fingerprint,actor_user_id,
    invoked_by_role,details
  ) VALUES (
    memory.atomic_span_uuid_v1(concat_ws('|',
      'contextual-split-terminal-v3',actor::text,p_parent_evidence_id::text,
      p_selector_version,p_expected_plan_sha256
    )),
    actor,p_parent_evidence_id,p_selector_version,'skipped',
    'contextual_split_parent',p_expected_parent_content_sha256,
    NULL,NULL,NULL,fingerprint,actor,session_user,jsonb_build_object(
      'route','contextual_split',
      'splitter_version',
        'memory_v1_contextual_span_splitter_20260728_v3',
      'plan_sha256',p_expected_plan_sha256,
      'child_count',p_expected_child_count
    )
  )
  RETURNING * INTO inserted;
  RETURN QUERY SELECT
    inserted.terminal_id,inserted.outcome,'applied'::text,
    inserted.decision_fingerprint;
END
$function$;

ALTER FUNCTION memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
) OWNER TO memory_context_rebind_maintainer;
ALTER FUNCTION memory.apply_owner_contextual_split_v3(
  uuid,text,text,jsonb,text
) OWNER TO memory_evidence_maintainer;
ALTER FUNCTION memory.finalize_owner_contextual_split_v3(
  uuid,text,text,text,integer
) OWNER TO memory_intake_maintainer;

REVOKE ALL ON FUNCTION memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
) FROM PUBLIC,brains_app,memory_evidence_maintainer,
       memory_intake_maintainer,memory_context_rebind_maintainer,
       memory_extraction_queue_maintainer;
REVOKE ALL ON FUNCTION memory.apply_owner_contextual_split_v3(
  uuid,text,text,jsonb,text
) FROM PUBLIC,brains_app,memory_evidence_maintainer,
       memory_intake_maintainer,memory_context_rebind_maintainer,
       memory_extraction_queue_maintainer;
REVOKE ALL ON FUNCTION memory.finalize_owner_contextual_split_v3(
  uuid,text,text,text,integer
) FROM PUBLIC,brains_app,memory_evidence_maintainer,
       memory_intake_maintainer,memory_context_rebind_maintainer,
       memory_extraction_queue_maintainer;

GRANT EXECUTE ON FUNCTION memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
) TO memory_context_rebind_maintainer;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
) TO memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.apply_owner_contextual_split_v3(
  uuid,text,text,jsonb,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_contextual_split_v3(
  uuid,text,text,text,integer
) TO brains_app;

COMMIT;
