BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $rollback$
DECLARE
  target constant regprocedure :=
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '58c46d113d580376cef0b29fc670ab101e5e3aa8d0ef2095cf111373c8fea3fc';
  new_definition_sha constant text :=
    '31e7dd395df1f5b5575d4d65d4be62858c481ad2b845ad055de05e2b8bafd41a';
  old_fragment constant text :=
$old$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v11','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_fragment constant text :=
$new$       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v11','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v12','UTF8'
       ),'sha256'),'hex')
     )$new$;
  definition text;
  definition_sha text;
BEGIN
  IF current_user<>'sage' OR target IS NULL THEN
    RAISE EXCEPTION 'V5.2 compiler-v12 rollback prerequisites are absent';
  END IF;
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha:=encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF definition_sha=new_definition_sha THEN
    IF (length(definition)-length(replace(definition,new_fragment,'')))
         / length(new_fragment)<>1 THEN
      RAISE EXCEPTION 'V5.2 compiler-v12 rollback guard is not replaceable';
    END IF;
    definition:=replace(definition,new_fragment,old_fragment);
    IF encode(public.digest(convert_to(
         definition,'UTF8'
       ),'sha256'),'hex')<>old_definition_sha THEN
      RAISE EXCEPTION 'V5.2 compiler-v12 rollback changed';
    END IF;
    EXECUTE definition;
  ELSIF definition_sha=old_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.2 compiler-v12 rollback baseline changed: %',
      definition_sha USING ERRCODE='23514';
  END IF;
END
$rollback$;

COMMIT;
