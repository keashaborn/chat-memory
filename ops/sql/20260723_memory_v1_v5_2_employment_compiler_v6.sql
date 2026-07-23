BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '419c80b92a4ed8197a7f7ae9c7780fc0f6d3bb69d98d2cc0eea3379306c2195c';
  new_definition_sha constant text :=
    '39715a76bec7dceebbe1b15e25d2fff8300d42331a8bfb6c557070fa4f3e53ae';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v5','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v5','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v6','UTF8'
       ),'sha256'),'hex')
     )$new$;
  definition text;
  definition_sha text;
BEGIN
  IF current_user<>'sage' OR target IS NULL THEN
    RAISE EXCEPTION 'V5.2 compiler-v6 persistence prerequisites are absent';
  END IF;
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha=old_definition_sha THEN
    IF (length(definition)-length(replace(definition,old_fragment,'')))
         / length(old_fragment)<>1
       OR position(new_fragment IN definition)<>0 THEN
      RAISE EXCEPTION 'V5.2 compiler-v6 persistence guard is not replaceable'
        USING ERRCODE='23514';
    END IF;
    definition:=replace(definition,old_fragment,new_fragment);
    definition_sha:=encode(public.digest(
      convert_to(definition,'UTF8'),'sha256'
    ),'hex');
    IF definition_sha<>new_definition_sha THEN
      RAISE EXCEPTION 'V5.2 compiler-v6 replacement changed: %',
        definition_sha USING ERRCODE='23514';
    END IF;
    EXECUTE definition;
  ELSIF definition_sha=new_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.2 compiler-v6 persistence baseline changed: %',
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
