BEGIN;
DO $persistence_compatibility$
DECLARE
  persistence_oid constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_before constant text :=
    '78ee8c17d613e9db9d3c040c2e73ed28f50f0b9898cecca7ea5d892c91a95830';
  expected_after constant text :=
    '0ca8e0240ce8d4d9c1d43f3457c4e2b2a6475176c47a240059f644125af41f49';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v4','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v3','UTF8'
       ),'sha256'),'hex')
     )$new$;
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(persistence_oid) INTO source;
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha=expected_after THEN
    RETURN;
  END IF;
  IF source_sha<>expected_before
     OR (length(source)-length(replace(source,old_fragment,'')))
          / length(old_fragment)<>1 THEN
    RAISE EXCEPTION 'V5.2 persistence v4 rollback baseline changed: %',
      source_sha USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_fragment,new_fragment);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_after THEN
    RAISE EXCEPTION 'V5.2 persistence v4 rollback changed: %',source_sha
      USING ERRCODE='23514';
  END IF;
  EXECUTE source;
END
$persistence_compatibility$;

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

DROP FUNCTION IF EXISTS memory.enqueue_owner_v5_2_stance_schema_reextract_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_stance_schema_reextract_v1(uuid);
COMMIT;
