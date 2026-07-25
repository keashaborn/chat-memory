BEGIN;

DO $rollback$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '4feb28bcf1a132a2c6f4b3628d11b3287db9c009090fd1a9ef68289c5af2f37e';
  new_definition_sha constant text :=
    '39715a76bec7dceebbe1b15e25d2fff8300d42331a8bfb6c557070fa4f3e53ae';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v6','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v7','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v6','UTF8'
       ),'sha256'),'hex')
     )$new$;
  definition text;
  definition_sha text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha<>old_definition_sha THEN
    RAISE EXCEPTION 'V5.2 compiler-v7 rollback baseline changed: %',
      definition_sha USING ERRCODE='23514';
  END IF;
  IF (length(definition)-length(replace(definition,old_fragment,'')))
       / length(old_fragment)<>1 THEN
    RAISE EXCEPTION 'V5.2 compiler-v7 rollback guard is not replaceable'
      USING ERRCODE='23514';
  END IF;
  definition:=replace(definition,old_fragment,new_fragment);
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha<>new_definition_sha THEN
    RAISE EXCEPTION 'V5.2 compiler-v7 rollback changed: %',
      definition_sha USING ERRCODE='23514';
  END IF;
  EXECUTE definition;
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

DROP FUNCTION IF EXISTS memory.requeue_owner_local_persistence_failure_v1(
  uuid,uuid,text,uuid,uuid,integer,text
);

COMMIT;
