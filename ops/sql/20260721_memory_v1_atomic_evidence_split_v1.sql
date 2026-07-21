BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_evidence_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.v5_jsonb_exact_keys(jsonb,text[])') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_evidence_intake_v1(text,integer,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'atomic evidence split prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.evidence_atomic_span_v1 (
  span_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  parent_evidence_id uuid NOT NULL,
  child_evidence_id uuid NOT NULL,
  splitter_version text NOT NULL,
  parent_content_sha256 text NOT NULL,
  child_content_sha256 text NOT NULL,
  ordinal smallint NOT NULL,
  char_start integer NOT NULL,
  char_end integer NOT NULL,
  boundary_reason text NOT NULL,
  plan_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_user_id,span_id),
  UNIQUE(owner_user_id,parent_evidence_id,splitter_version,ordinal),
  UNIQUE(owner_user_id,parent_evidence_id,splitter_version,char_start,char_end),
  UNIQUE(owner_user_id,child_evidence_id),
  FOREIGN KEY(owner_user_id,parent_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,child_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id) ON DELETE RESTRICT,
  CHECK(splitter_version='memory_v1_sentence_splitter_v1'),
  CHECK(parent_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK(child_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK(plan_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK(ordinal BETWEEN 0 AND 19),
  CHECK(char_start>=0 AND char_end>char_start),
  CHECK(boundary_reason IN ('sentence_boundary','terminal_span'))
);

ALTER TABLE memory.evidence_atomic_span_v1 OWNER TO sage;
ALTER TABLE memory.evidence_atomic_span_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence_atomic_span_v1 FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.evidence_atomic_span_v1;
CREATE POLICY owner_isolation ON memory.evidence_atomic_span_v1
  USING(owner_user_id=memory.current_actor_user_id())
  WITH CHECK(owner_user_id=memory.current_actor_user_id());
DROP TRIGGER IF EXISTS evidence_atomic_span_v1_append_only
  ON memory.evidence_atomic_span_v1;
CREATE TRIGGER evidence_atomic_span_v1_append_only
BEFORE UPDATE OR DELETE ON memory.evidence_atomic_span_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

CREATE OR REPLACE FUNCTION memory.atomic_span_uuid_v1(p_identity text)
RETURNS uuid
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path='pg_catalog'
AS $function$
  WITH digest_value AS (
    SELECT encode(public.digest(convert_to(p_identity,'UTF8'),'sha256'),'hex') AS h
  )
  SELECT (
    substr(h,1,8)||'-'||substr(h,9,4)||'-5'||substr(h,14,3)||'-8'||
    substr(h,18,3)||'-'||substr(h,21,12)
  )::uuid
  FROM digest_value;
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_owner_atomic_evidence_split_v1(
  p_parent_evidence_id uuid,
  p_expected_parent_content_sha256 text,
  p_splitter_version text,
  p_spans jsonb
)
RETURNS TABLE(plan_sha256 text,span_count integer,span_plan jsonb)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path='pg_catalog'
AS $function$
DECLARE
  actor uuid;
  parent memory.evidence%ROWTYPE;
  item jsonb;
  plan jsonb := '[]'::jsonb;
  ordinal_value integer := 0;
  start_value integer;
  end_value integer;
  content_value text;
  content_hash text;
  reason_value text;
  previous_end integer := 0;
  child_id uuid;
  span_id_value uuid;
  external_id_value text;
  wrapper jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'atomic split preflight requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_parent_evidence_id IS NULL
     OR p_expected_parent_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_splitter_version<>'memory_v1_sentence_splitter_v1'
     OR jsonb_typeof(p_spans)<>'array'
     OR jsonb_array_length(p_spans)<1
     OR jsonb_array_length(p_spans)>20 THEN
    RAISE EXCEPTION 'atomic split preflight inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT evidence.* INTO parent
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_parent_evidence_id;
  IF NOT FOUND OR parent.status<>'active'
     OR parent.kind NOT IN ('user_statement','external_observation')
     OR parent.source_system<>'public.chat_log'
     OR parent.content IS NULL OR btrim(parent.content)=''
     OR parent.content_sha256 IS DISTINCT FROM p_expected_parent_content_sha256
     OR parent.metadata ? 'atomic_parent_evidence_id' THEN
    RAISE EXCEPTION 'owner-scoped atomic split parent is unavailable'
      USING ERRCODE='P0002';
  END IF;

  FOR item IN
    SELECT value FROM jsonb_array_elements(p_spans) AS rows(value)
    ORDER BY (value->>'ordinal')::integer
  LOOP
    IF NOT memory.v5_jsonb_exact_keys(item,ARRAY[
      'ordinal','char_start','char_end','content','content_sha256',
      'boundary_reason'
    ]) OR jsonb_typeof(item->'ordinal')<>'number'
       OR jsonb_typeof(item->'char_start')<>'number'
       OR jsonb_typeof(item->'char_end')<>'number'
       OR jsonb_typeof(item->'content')<>'string'
       OR jsonb_typeof(item->'content_sha256')<>'string'
       OR jsonb_typeof(item->'boundary_reason')<>'string' THEN
      RAISE EXCEPTION 'atomic split span shape is invalid'
        USING ERRCODE='22023';
    END IF;
    IF (item->>'ordinal')::integer<>ordinal_value THEN
      RAISE EXCEPTION 'atomic split ordinals must be contiguous from zero'
        USING ERRCODE='23514';
    END IF;
    start_value := (item->>'char_start')::integer;
    end_value := (item->>'char_end')::integer;
    content_value := item->>'content';
    content_hash := item->>'content_sha256';
    reason_value := item->>'boundary_reason';
    IF start_value<previous_end OR end_value<=start_value
       OR end_value>length(parent.content)
       OR length(content_value)>600 OR btrim(content_value)=''
       OR reason_value NOT IN ('sentence_boundary','terminal_span')
       OR content_hash !~ '^[0-9a-f]{64}$'
       OR substring(parent.content FROM start_value+1 FOR end_value-start_value)
            IS DISTINCT FROM content_value
       OR encode(public.digest(convert_to(content_value,'UTF8'),'sha256'),'hex')
            IS DISTINCT FROM content_hash
       OR regexp_replace(
            substring(parent.content FROM previous_end+1 FOR start_value-previous_end),
            '[[:space:]]','','g'
          )<>'' THEN
      RAISE EXCEPTION 'atomic split span content or coverage is invalid'
        USING ERRCODE='23514';
    END IF;
    child_id := memory.atomic_span_uuid_v1(concat_ws('|',
      'child',actor::text,parent.evidence_id::text,parent.content_sha256,
      p_splitter_version,ordinal_value::text,start_value::text,end_value::text,
      content_hash
    ));
    span_id_value := memory.atomic_span_uuid_v1(concat_ws('|',
      'span',actor::text,parent.evidence_id::text,parent.content_sha256,
      p_splitter_version,ordinal_value::text,start_value::text,end_value::text,
      content_hash
    ));
    external_id_value := concat_ws(':','atomic',parent.evidence_id::text,
      'sentence-v1',lpad(ordinal_value::text,2,'0'),substr(content_hash,1,16));
    plan := plan || jsonb_build_array(jsonb_build_object(
      'ordinal',ordinal_value,
      'span_id',span_id_value,
      'child_evidence_id',child_id,
      'external_id',external_id_value,
      'char_start',start_value,
      'char_end',end_value,
      'content',content_value,
      'content_sha256',content_hash,
      'boundary_reason',reason_value
    ));
    previous_end := end_value;
    ordinal_value := ordinal_value+1;
  END LOOP;
  IF ordinal_value<>jsonb_array_length(p_spans)
     OR regexp_replace(substring(parent.content FROM previous_end+1),
          '[[:space:]]','','g')<>'' THEN
    RAISE EXCEPTION 'atomic split source coverage is incomplete'
      USING ERRCODE='23514';
  END IF;

  wrapper := jsonb_build_object(
    'contract_version','memory_v1_atomic_evidence_split_plan_v1',
    'owner_user_id',actor,
    'parent_evidence_id',parent.evidence_id,
    'parent_content_sha256',parent.content_sha256,
    'splitter_version',p_splitter_version,
    'spans',plan
  );
  RETURN QUERY SELECT
    encode(public.digest(convert_to(wrapper::text,'UTF8'),'sha256'),'hex'),
    ordinal_value,
    plan;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_owner_atomic_evidence_split_v1(
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
  existing_evidence memory.evidence%ROWTYPE;
  existing_span memory.evidence_atomic_span_v1%ROWTYPE;
  row_outcome text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'atomic split apply requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_expected_plan_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'atomic split apply inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'atomic_evidence_split',actor::text,p_parent_evidence_id::text,
    p_splitter_version
  ),0));
  SELECT * INTO planned
  FROM memory.preflight_owner_atomic_evidence_split_v1(
    p_parent_evidence_id,p_expected_parent_content_sha256,
    p_splitter_version,p_spans
  );
  IF planned.plan_sha256 IS DISTINCT FROM p_expected_plan_sha256 THEN
    RAISE EXCEPTION 'atomic split plan SHA-256 changed'
      USING ERRCODE='23514';
  END IF;
  SELECT evidence.* INTO STRICT parent
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_parent_evidence_id;

  FOR item IN
    SELECT value FROM jsonb_array_elements(planned.span_plan) AS rows(value)
    ORDER BY (value->>'ordinal')::integer
  LOOP
    row_outcome := 'applied';
    SELECT evidence.* INTO existing_evidence
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id=actor
      AND (
        evidence.evidence_id=(item->>'child_evidence_id')::uuid
        OR (evidence.source_system=parent.source_system
            AND evidence.external_id=item->>'external_id')
      );
    IF FOUND THEN
      IF existing_evidence.evidence_id<>(item->>'child_evidence_id')::uuid
         OR existing_evidence.kind<>parent.kind
         OR existing_evidence.source_system<>parent.source_system
         OR existing_evidence.external_id<>(item->>'external_id')
         OR existing_evidence.content IS DISTINCT FROM (item->>'content')
         OR existing_evidence.content_sha256 IS DISTINCT FROM (item->>'content_sha256')
         OR existing_evidence.observed_at IS DISTINCT FROM parent.observed_at
         OR existing_evidence.recorded_at IS DISTINCT FROM parent.recorded_at
         OR existing_evidence.directness IS DISTINCT FROM parent.directness
         OR existing_evidence.source_reliability IS DISTINCT FROM parent.source_reliability
         OR existing_evidence.independence_key IS DISTINCT FROM parent.independence_key
         OR existing_evidence.sensitivity IS DISTINCT FROM parent.sensitivity
         OR existing_evidence.status<>'active'
         OR existing_evidence.metadata IS DISTINCT FROM jsonb_build_object(
           'atomic_parent_evidence_id',parent.evidence_id,
           'atomic_parent_content_sha256',parent.content_sha256,
           'atomic_splitter_version',p_splitter_version,
           'atomic_span_id',(item->>'span_id')::uuid,
           'atomic_ordinal',(item->>'ordinal')::integer,
           'atomic_char_start',(item->>'char_start')::integer,
           'atomic_char_end',(item->>'char_end')::integer,
           'atomic_boundary_reason',item->>'boundary_reason',
           'atomic_plan_sha256',planned.plan_sha256
         ) THEN
        RAISE EXCEPTION 'atomic child evidence replay conflicts'
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
        parent.sensitivity,'active',jsonb_build_object(
          'atomic_parent_evidence_id',parent.evidence_id,
          'atomic_parent_content_sha256',parent.content_sha256,
          'atomic_splitter_version',p_splitter_version,
          'atomic_span_id',(item->>'span_id')::uuid,
          'atomic_ordinal',(item->>'ordinal')::integer,
          'atomic_char_start',(item->>'char_start')::integer,
          'atomic_char_end',(item->>'char_end')::integer,
          'atomic_boundary_reason',item->>'boundary_reason',
          'atomic_plan_sha256',planned.plan_sha256
        )
      );
    END IF;

    SELECT span.* INTO existing_span
    FROM memory.evidence_atomic_span_v1 AS span
    WHERE span.owner_user_id=actor
      AND span.span_id=(item->>'span_id')::uuid;
    IF FOUND THEN
      IF existing_span.parent_evidence_id<>parent.evidence_id
         OR existing_span.child_evidence_id<>(item->>'child_evidence_id')::uuid
         OR existing_span.splitter_version<>p_splitter_version
         OR existing_span.parent_content_sha256<>parent.content_sha256
         OR existing_span.child_content_sha256<>(item->>'content_sha256')
         OR existing_span.ordinal<>(item->>'ordinal')::integer
         OR existing_span.char_start<>(item->>'char_start')::integer
         OR existing_span.char_end<>(item->>'char_end')::integer
         OR existing_span.boundary_reason<>(item->>'boundary_reason')
         OR existing_span.plan_sha256<>planned.plan_sha256 THEN
        RAISE EXCEPTION 'atomic span replay conflicts'
          USING ERRCODE='23514';
      END IF;
    ELSE
      INSERT INTO memory.evidence_atomic_span_v1(
        span_id,owner_user_id,parent_evidence_id,child_evidence_id,
        splitter_version,parent_content_sha256,child_content_sha256,ordinal,
        char_start,char_end,boundary_reason,plan_sha256
      ) VALUES (
        (item->>'span_id')::uuid,actor,parent.evidence_id,
        (item->>'child_evidence_id')::uuid,p_splitter_version,
        parent.content_sha256,item->>'content_sha256',
        (item->>'ordinal')::integer,(item->>'char_start')::integer,
        (item->>'char_end')::integer,item->>'boundary_reason',
        planned.plan_sha256
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

GRANT SELECT,INSERT ON memory.evidence_atomic_span_v1
  TO memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  TO memory_evidence_maintainer;
ALTER FUNCTION memory.atomic_span_uuid_v1(text) OWNER TO sage;
ALTER FUNCTION memory.preflight_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb
) OWNER TO memory_evidence_maintainer;
ALTER FUNCTION memory.apply_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb,text
) OWNER TO memory_evidence_maintainer;

REVOKE ALL ON memory.evidence_atomic_span_v1 FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.atomic_span_uuid_v1(text)
  FROM PUBLIC,brains_app,memory_evidence_maintainer;
REVOKE ALL ON FUNCTION memory.preflight_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb
) FROM PUBLIC,brains_app,memory_evidence_maintainer;
REVOKE ALL ON FUNCTION memory.apply_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb,text
) FROM PUBLIC,brains_app,memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.atomic_span_uuid_v1(text)
  TO memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.preflight_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb
) TO brains_app,memory_evidence_maintainer;
GRANT EXECUTE ON FUNCTION memory.apply_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb,text
) TO brains_app;

COMMIT;
