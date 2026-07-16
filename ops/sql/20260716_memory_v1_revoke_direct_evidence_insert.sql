BEGIN;

DO $preflight$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_evidence_v1(memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,memory.sensitivity_level,jsonb)';
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence privilege migration requires sage';
  END IF;
  IF to_regclass('memory.evidence') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regrole('memory_evidence_maintainer') IS NULL
     OR to_regprocedure(function_oid::text) IS NULL THEN
    RAISE EXCEPTION 'controlled evidence writer prerequisites are absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid=function_oid
      AND prosecdef
      AND proowner='memory_evidence_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'controlled evidence writer is not fail-closed';
  END IF;
  IF NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR NOT has_table_privilege('brains_app','memory.evidence','SELECT')
     OR NOT has_table_privilege(
       'memory_evidence_maintainer','memory.evidence','INSERT'
     ) THEN
    RAISE EXCEPTION 'controlled evidence writer privileges are incomplete';
  END IF;
  IF has_table_privilege('brains_app','memory.evidence','UPDATE')
     OR has_table_privilege('brains_app','memory.evidence','DELETE') THEN
    RAISE EXCEPTION 'brains_app has unsafe evidence mutation privileges';
  END IF;
END
$preflight$;

REVOKE INSERT ON memory.evidence FROM brains_app;

DO $postflight$
BEGIN
  IF has_table_privilege('brains_app','memory.evidence','INSERT')
     OR NOT has_table_privilege('brains_app','memory.evidence','SELECT') THEN
    RAISE EXCEPTION 'direct evidence INSERT was not removed cleanly';
  END IF;
END
$postflight$;

COMMIT;
