BEGIN;

DO $migration$
DECLARE
  function_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  expected_before constant text :=
    'cea8bc6ee880a4897b975191a80ff35a36fb22751c1bb2bd601e7836f620cd29';
  expected_after constant text :=
    '62175d9205544eaae20521dbb819d9c8c0838fbb94287c8abacdaed61ab93160';
  old_fragment constant text :=
    '      AND event.policy_compiler_sha256=p_policy_compiler_sha256';
  new_fragment constant text :=
    '      AND event.policy_compiler_sha256=p_policy_compiler_sha256' || E'\n' ||
    '      AND event.rejection_code IS DISTINCT FROM ''local_worker_abandoned''';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_before
     OR (length(source)-length(replace(source,old_fragment,'')))
          / length(old_fragment)<>1 THEN
    RAISE EXCEPTION 'local orphan circuit baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_fragment,new_fragment);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_after THEN
    RAISE EXCEPTION 'local orphan circuit replacement changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$migration$;

ALTER FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) TO brains_app;

COMMIT;
