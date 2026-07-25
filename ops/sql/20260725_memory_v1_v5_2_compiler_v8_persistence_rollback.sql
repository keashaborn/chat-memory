BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $rollback$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '4feb28bcf1a132a2c6f4b3628d11b3287db9c009090fd1a9ef68289c5af2f37e';
  new_definition_sha constant text :=
    '4dcbd998364abc91d5f6abd0844546c74387f4c057fc876efbee7c222eb0c8ab';
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
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v7','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v8','UTF8'
       ),'sha256'),'hex')
     )$new$;
  definition text;
  definition_sha text;
BEGIN
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha=new_definition_sha THEN
    IF (length(definition)-length(replace(definition,new_fragment,'')))
         / length(new_fragment)<>1 THEN
      RAISE EXCEPTION 'V5.2 compiler-v8 rollback guard is not replaceable';
    END IF;
    definition:=replace(definition,new_fragment,old_fragment);
    IF encode(public.digest(convert_to(
         definition,'UTF8'
       ),'sha256'),'hex')<>old_definition_sha THEN
      RAISE EXCEPTION 'V5.2 compiler-v8 rollback changed';
    END IF;
    EXECUTE definition;
  ELSIF definition_sha=old_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.2 compiler-v8 rollback baseline changed: %',
      definition_sha;
  END IF;
END
$rollback$;

COMMIT;
