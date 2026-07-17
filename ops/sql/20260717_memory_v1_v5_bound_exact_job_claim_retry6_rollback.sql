BEGIN;

DO $rollback$
DECLARE
  function_oid regprocedure :=
    'memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'::regprocedure;
  function_definition text;
  before_sha text;
  after_sha text;
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'exact-job retry6 rollback requires sage';
  END IF;

  SELECT pg_get_functiondef(function_oid) INTO function_definition;
  before_sha := encode(public.digest(
    convert_to(function_definition,'UTF8'),'sha256'
  ),'hex');
  IF before_sha<>'9768f3cf0d96dcc926a94a3f4d4260e6ada33eb45714851757e0604ce961ef6a'
     OR (length(function_definition)-length(replace(
       function_definition,
       'p_max_attempts NOT BETWEEN 1 AND 6',''
     )))<>length('p_max_attempts NOT BETWEEN 1 AND 6') THEN
    RAISE EXCEPTION 'exact-job claim function drifted before retry6 rollback';
  END IF;

  EXECUTE replace(
    function_definition,
    'p_max_attempts NOT BETWEEN 1 AND 6',
    'p_max_attempts NOT BETWEEN 1 AND 5'
  );

  SELECT encode(public.digest(
    convert_to(pg_get_functiondef(function_oid),'UTF8'),'sha256'
  ),'hex') INTO after_sha;
  IF after_sha<>'e67b5bbd08e9eb40283b29ee436f8aec6e3633439d65fb5897d2bc9ef9938b09'
     OR (SELECT proowner::regrole::text
         FROM pg_proc WHERE oid=function_oid)<>'memory_extraction_worker_maintainer'
     OR NOT (SELECT prosecdef FROM pg_proc WHERE oid=function_oid)
     OR NOT has_function_privilege('brains_app',function_oid,'EXECUTE')
     OR has_function_privilege('public',function_oid,'EXECUTE') THEN
    RAISE EXCEPTION 'exact-job retry6 rollback postcondition failed';
  END IF;
END
$rollback$;

COMMIT;
