BEGIN;
DO $persistence_compatibility$
DECLARE
  persistence_oid constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  expected_before constant text :=
    '419c80b92a4ed8197a7f7ae9c7780fc0f6d3bb69d98d2cc0eea3379306c2195c';
  expected_after constant text :=
    '78ee8c17d613e9db9d3c040c2e73ed28f50f0b9898cecca7ea5d892c91a95830';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v4','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v5','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v4','UTF8'
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
    RAISE EXCEPTION 'V5.2 persistence v5 rollback baseline changed: %',
      source_sha USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_fragment,new_fragment);
  source_sha:=encode(public.digest(convert_to(source,'UTF8'),'sha256'),'hex');
  IF source_sha<>expected_after THEN
    RAISE EXCEPTION 'V5.2 persistence v5 rollback changed: %',source_sha
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

DROP FUNCTION IF EXISTS memory.enqueue_owner_v5_2_atomic_stance_reextract_v2(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_atomic_stance_reextract_v2(uuid);
COMMIT;
