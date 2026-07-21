\set ON_ERROR_STOP on

BEGIN;

DO $security$
DECLARE
  table_oid oid := 'memory.evidence_atomic_span_v1'::regclass;
  preflight_oid oid :=
    'memory.preflight_owner_atomic_evidence_split_v1(uuid,text,text,jsonb)'::regprocedure;
  apply_oid oid :=
    'memory.apply_owner_atomic_evidence_split_v1(uuid,text,text,jsonb,text)'::regprocedure;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid=table_oid AND relrowsecurity AND relforcerowsecurity
      AND relowner='sage'::regrole
  ) THEN
    RAISE EXCEPTION 'atomic span table ownership or RLS is unsafe';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid=table_oid
      AND tgname='evidence_atomic_span_v1_append_only'
      AND tgenabled='O'
  ) THEN
    RAISE EXCEPTION 'atomic span append-only trigger is absent';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_proc AS procedure
    CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
    WHERE procedure.oid IN (preflight_oid,apply_oid)
      AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
  ) OR NOT has_function_privilege('brains_app',preflight_oid,'EXECUTE')
     OR NOT has_function_privilege('brains_app',apply_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'atomic split function ACL is unsafe';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_proc
    WHERE oid IN (preflight_oid,apply_oid)
      AND (
        NOT prosecdef
        OR proowner<>'memory_evidence_maintainer'::regrole
        OR NOT EXISTS (
          SELECT 1 FROM unnest(proconfig) AS setting
          WHERE setting='search_path=pg_catalog'
        )
      )
  ) THEN
    RAISE EXCEPTION 'atomic split function security contract is unsafe';
  END IF;
END
$security$;

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,observed_at,recorded_at,directness,source_reliability,
  independence_key,sensitivity,status,metadata
) VALUES (
  'a7000000-0000-4000-8000-000000000001',
  '11111111-1111-4111-8111-111111111111',
  'user_statement','public.chat_log','atomic-split-rollback-fixture-v1',
  'My mother died in March. My father lives nearby.',
  encode(public.digest(convert_to(
    'My mother died in March. My father lives nearby.','UTF8'
  ),'sha256'),'hex'),
  '2026-03-20T12:00:00Z','2026-03-20T12:00:01Z',1.0,1.0,
  'atomic-split-rollback-fixture-v1','medium','active','{}'::jsonb
);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','11111111-1111-4111-8111-111111111111',false
);

DO $owner_apply$
DECLARE
  source_text constant text :=
    'My mother died in March. My father lives nearby.';
  source_hash text := encode(
    public.digest(convert_to(source_text,'UTF8'),'sha256'),'hex'
  );
  span_plan jsonb;
  locked_plan_sha text;
  applied_count integer;
  replayed_count integer;
BEGIN
  span_plan := jsonb_build_array(
    jsonb_build_object(
      'ordinal',0,'char_start',0,'char_end',24,
      'content',substring(source_text FROM 1 FOR 24),
      'content_sha256',encode(public.digest(
        convert_to(substring(source_text FROM 1 FOR 24),'UTF8'),
        'sha256'
      ),'hex'),
      'boundary_reason','sentence_boundary'
    ),
    jsonb_build_object(
      'ordinal',1,'char_start',25,'char_end',48,
      'content',substring(source_text FROM 26 FOR 23),
      'content_sha256',encode(public.digest(
        convert_to(substring(source_text FROM 26 FOR 23),'UTF8'),
        'sha256'
      ),'hex'),
      'boundary_reason','terminal_span'
    )
  );
  SELECT plan.plan_sha256 INTO STRICT locked_plan_sha
  FROM memory.preflight_owner_atomic_evidence_split_v1(
    'a7000000-0000-4000-8000-000000000001',source_hash,
    'memory_v1_sentence_splitter_v1',span_plan
  ) AS plan;
  SELECT count(*) FILTER (WHERE result.apply_outcome='applied')
  INTO STRICT applied_count
  FROM memory.apply_owner_atomic_evidence_split_v1(
    'a7000000-0000-4000-8000-000000000001',source_hash,
    'memory_v1_sentence_splitter_v1',span_plan,locked_plan_sha
  ) AS result;
  SELECT count(*) FILTER (WHERE result.apply_outcome='replayed')
  INTO STRICT replayed_count
  FROM memory.apply_owner_atomic_evidence_split_v1(
    'a7000000-0000-4000-8000-000000000001',source_hash,
    'memory_v1_sentence_splitter_v1',span_plan,locked_plan_sha
  ) AS result;
  IF applied_count<>2 OR replayed_count<>2 THEN
    RAISE EXCEPTION 'atomic split apply/replay counts changed';
  END IF;
END
$owner_apply$;

SELECT set_config(
  'app.user_id','22222222-2222-4222-8222-222222222222',false
);
DO $cross_owner$
DECLARE
  source_text constant text :=
    'My mother died in March. My father lives nearby.';
  source_hash text := encode(
    public.digest(convert_to(source_text,'UTF8'),'sha256'),'hex'
  );
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_owner_atomic_evidence_split_v1(
      'a7000000-0000-4000-8000-000000000001',source_hash,
      'memory_v1_sentence_splitter_v1',jsonb_build_array(
        jsonb_build_object(
          'ordinal',0,'char_start',0,'char_end',length(source_text),
          'content',source_text,
          'content_sha256',source_hash,
          'boundary_reason','terminal_span'
        )
      )
    );
    RAISE EXCEPTION 'cross-owner preflight unexpectedly succeeded';
  EXCEPTION WHEN no_data_found THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;

DO $postconditions$
DECLARE
  owner_rows integer;
  other_rows integer;
BEGIN
  SELECT count(*) INTO owner_rows
  FROM memory.evidence_atomic_span_v1
  WHERE owner_user_id='11111111-1111-4111-8111-111111111111';
  SELECT count(*) INTO other_rows
  FROM memory.evidence_atomic_span_v1
  WHERE owner_user_id='22222222-2222-4222-8222-222222222222';
  IF owner_rows<>2 OR other_rows<>0 THEN
    RAISE EXCEPTION 'atomic split owner isolation changed';
  END IF;
  BEGIN
    UPDATE memory.evidence_atomic_span_v1
    SET ordinal=ordinal
    WHERE owner_user_id='11111111-1111-4111-8111-111111111111';
    RAISE EXCEPTION 'atomic span update unexpectedly succeeded';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM='atomic span update unexpectedly succeeded' THEN
      RAISE;
    END IF;
  END;
END
$postconditions$;

ROLLBACK;

SELECT 'memory_v1_atomic_evidence_split_v1: PASS' AS result;
