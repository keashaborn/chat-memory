BEGIN;

DO $rollback$
DECLARE
  function_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  expected_before constant text :=
    '01052eb7ea5a2add2a9795bcc960408a7702fdc1baee83e17b12722889307970';
  expected_after constant text :=
    '9d10f1507b2a619ad011c7033076af8c52b811fc3cdaae5c2f9b79a6f3fb56fb';
  old_fragment constant text :=
    '    WHERE event.owner_user_id=actor AND event.action=''completed''' || E'\n' ||
    '      AND event.provider_id=p_provider_id' || E'\n' ||
    '      AND event.provider_version=p_provider_version' || E'\n' ||
    '      AND event.provider_model_sha256=p_provider_model_sha256' || E'\n' ||
    '      AND event.model_file_sha256=p_model_file_sha256' || E'\n' ||
    '      AND event.runtime_revision_sha256=p_runtime_revision_sha256' || E'\n' ||
    '      AND event.policy_compiler_sha256=p_policy_compiler_sha256';
  new_fragment constant text :=
    '    WHERE event.owner_user_id=actor AND event.action=''completed''';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  source_sha := encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_before THEN
    RAISE EXCEPTION 'local circuit rollback baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  IF (length(source)-length(replace(source,old_fragment,'')))
       / length(old_fragment)<>1 THEN
    RAISE EXCEPTION 'local circuit rollback anchor is not unique'
      USING ERRCODE='23514';
  END IF;
  source := replace(source,old_fragment,new_fragment);
  source_sha := encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_after THEN
    RAISE EXCEPTION 'local circuit rollback hash changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$rollback$;

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
