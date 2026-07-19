BEGIN;

DO $patch$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)';
  definition text;
  old_fragment text := E'     OR NOT packet.manual_review_required\n';
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'relational review admission compatibility requires sage';
  END IF;
  IF to_regprocedure(function_oid::text) IS NULL THEN
    RAISE EXCEPTION 'local review artifact writer is absent';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid=function_oid
      AND prosecdef
      AND proowner='memory_v5_local_disposition_maintainer'::regrole
      AND proconfig=ARRAY['search_path=pg_catalog']::text[]
  ) THEN
    RAISE EXCEPTION 'local review artifact writer is not fail-closed';
  END IF;

  definition := pg_get_functiondef(function_oid);
  IF position(old_fragment IN definition)>0 THEN
    EXECUTE replace(definition,old_fragment,'');
  ELSIF position('NOT packet.manual_review_required' IN definition)>0 THEN
    RAISE EXCEPTION 'manual-review gate has an unknown definition';
  END IF;
END
$patch$;

DO $postflight$
DECLARE
  function_oid regprocedure :=
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)';
  definition text := pg_get_functiondef(
    'memory.record_owner_v5_local_review_artifact_v1(uuid,uuid,uuid,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'::regprocedure
  );
BEGIN
  IF position('NOT packet.manual_review_required' IN definition)>0
     OR position(
       'packet.entity_mention_count+packet.observation_count' IN definition
     )=0
     OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'relational review admission compatibility is incomplete';
  END IF;
END
$postflight$;

COMMIT;
