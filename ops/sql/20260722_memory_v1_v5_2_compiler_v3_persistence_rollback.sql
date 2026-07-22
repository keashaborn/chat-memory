BEGIN;

DROP FUNCTION IF EXISTS memory.requeue_owner_v5_2_persistence_mismatch_v1(
  uuid,uuid,text,uuid,uuid,integer,text
);

DO $rollback$
DECLARE
  persistence_oid constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  claim_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  persistence_before constant text :=
    '0ca8e0240ce8d4d9c1d43f3457c4e2b2a6475176c47a240059f644125af41f49';
  persistence_after constant text :=
    'a8a8e97bba56577a1f7bf0fbee932b1094b4e6c50200891eedb7c1970bcec3fd';
  claim_before constant text :=
    'cea8bc6ee880a4897b975191a80ff35a36fb22751c1bb2bd601e7836f620cd29';
  claim_after constant text :=
    '01052eb7ea5a2add2a9795bcc960408a7702fdc1baee83e17b12722889307970';
  persistence_old constant text :=
$old$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex')
     )$old$;
  persistence_new constant text :=
$new$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex')
     )$new$;
  claim_old constant text := '     OR p_max_attempts NOT BETWEEN 1 AND 4';
  claim_new constant text := '     OR p_max_attempts NOT BETWEEN 1 AND 3';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(persistence_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>persistence_before THEN
    RAISE EXCEPTION 'V5.2 persistence rollback baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,persistence_old,persistence_new);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>persistence_after THEN
    RAISE EXCEPTION 'V5.2 persistence rollback changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;

  SELECT pg_get_functiondef(claim_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>claim_before THEN
    RAISE EXCEPTION 'V5.2 claim rollback baseline changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,claim_old,claim_new);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>claim_after THEN
    RAISE EXCEPTION 'V5.2 claim rollback changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$rollback$;

ALTER FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_2_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

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
