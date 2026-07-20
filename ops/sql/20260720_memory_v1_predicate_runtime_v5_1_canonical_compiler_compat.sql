BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  target regprocedure :=
    'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure;
  old_definition_sha constant text :=
    '693c5623530d32451198699360072dfc7475fa1bafcf134267a5528a38ac087f';
  new_definition_sha constant text :=
    'e79079a29210cf508dfc34571a9bb9501271d4ae3ba4990c1fb910f3f8877a06';
  canonical_compiler_sha constant text :=
    'af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d';
  old_fragment constant text := $old$
     OR p_policy_compiler_sha256 <> encode(public.digest(convert_to(
       'memory_v1_relationship_policy_compiler_v14','UTF8'
     ),'sha256'),'hex')$old$;
  new_fragment constant text := $new$
     OR p_policy_compiler_sha256 <>
       'af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d'$new$;
  definition text;
  definition_sha text;
BEGIN
  IF current_user<>'sage' OR target IS NULL THEN
    RAISE EXCEPTION 'V5.1 canonical compiler compatibility prerequisites are absent';
  END IF;
  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha := encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');

  IF definition_sha=old_definition_sha THEN
    IF (length(definition)-length(replace(definition,old_fragment,'')))
         <>length(old_fragment)
       OR position(new_fragment IN definition)<>0 THEN
      RAISE EXCEPTION 'V5.1 persistence compiler guard is not uniquely replaceable';
    END IF;
    definition := replace(definition,old_fragment,new_fragment);
    EXECUTE definition;
  ELSIF definition_sha=new_definition_sha THEN
    NULL;
  ELSE
    RAISE EXCEPTION 'V5.1 persistence function definition drifted: %',
      definition_sha;
  END IF;

  SELECT pg_get_functiondef(target) INTO STRICT definition;
  definition_sha := encode(public.digest(
    convert_to(definition,'UTF8'),'sha256'
  ),'hex');
  IF position(new_fragment IN definition)=0
     OR position(old_fragment IN definition)<>0
     OR position(canonical_compiler_sha IN definition)=0 THEN
    RAISE EXCEPTION 'V5.1 canonical compiler guard was not installed';
  END IF;
  RAISE NOTICE 'V5.1 canonical compiler function sha256=%',definition_sha;
END
$migration$;

ALTER FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION memory.persist_owner_v5_1_local_packet_v1(
  uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,
  boolean,integer
) TO brains_app;

COMMIT;
