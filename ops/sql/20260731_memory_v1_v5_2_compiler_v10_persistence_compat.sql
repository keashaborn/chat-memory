BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '2b3182f59091a7d93697ae79ee6a8e1cc7541829f957b5a859c47cf28707d190';
  new_definition_sha constant text :=
    '8273a2de6dcdb4509c572580670d9bc34474b1b8988bc4172f9d29ae66995c3a';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v7','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v8','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v9','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v7','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v8','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v9','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v10','UTF8'
       ),'sha256'),'hex')
     )$new$;
  definition text;
  definition_sha text;
BEGIN
  IF current_user<>'sage' OR target IS NULL THEN
    RAISE EXCEPTION 'V5.2 compiler-v10 persistence prerequisites are absent';
  END IF;
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha=old_definition_sha THEN
    IF (length(definition)-length(replace(definition,old_fragment,'')))
         / length(old_fragment)<>1
       OR position(new_fragment IN definition)<>0 THEN
      RAISE EXCEPTION 'V5.2 compiler-v10 persistence guard is not replaceable'
        USING ERRCODE='23514';
    END IF;
    definition:=replace(definition,old_fragment,new_fragment);
    definition_sha:=encode(public.digest(
      convert_to(definition,'UTF8'),'sha256'
    ),'hex');
    IF definition_sha<>new_definition_sha THEN
      RAISE EXCEPTION 'V5.2 compiler-v10 replacement changed: %',
        definition_sha USING ERRCODE='23514';
    END IF;
    EXECUTE definition;
  ELSIF definition_sha=new_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.2 compiler-v10 persistence baseline changed: %',
      definition_sha USING ERRCODE='23514';
  END IF;
END
$migration$;

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

COMMIT;
