BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_claim_projection_admission AS admission
    JOIN memory.observation AS observation
      ON observation.owner_user_id=admission.owner_user_id
     AND observation.observation_id=admission.observation_id
    WHERE observation.predicate IN (
      'relationship.parent_of','relationship.sibling_of'
    )
  ) THEN
    RAISE EXCEPTION 'relationship claim projection rows exist; rollback refused';
  END IF;
END
$guard$;

DO $restore_functions$
DECLARE
  target regprocedure;
  definition text;
  old_list constant text :=
    '''identity.name'',''pet.breed'',''pet.sex'',''relationship.has_pet''';
  new_list constant text :=
    '''identity.name'',''pet.breed'',''pet.sex'',''relationship.has_pet'',''relationship.parent_of'',''relationship.sibling_of''';
  old_relation constant text :=
    'observation.predicate=''relationship.has_pet''';
  new_relation constant text :=
    'observation.predicate IN (''relationship.has_pet'',''relationship.parent_of'',''relationship.sibling_of'')';
BEGIN
  target:='memory.preflight_claim_projection_source_v5_1(uuid)'::regprocedure;
  definition:=pg_get_functiondef(target);
  IF position(new_list IN definition)>0 THEN
    IF position(new_relation IN definition)=0 THEN
      RAISE EXCEPTION 'claim source preflight relationship policy drifted';
    END IF;
    definition:=replace(definition,new_list,old_list);
    definition:=replace(definition,new_relation,old_relation);
    EXECUTE definition;
  ELSIF position(old_list IN definition)=0
     OR position(old_relation IN definition)=0 THEN
    RAISE EXCEPTION 'claim source preflight definition drifted';
  END IF;

  FOREACH target IN ARRAY ARRAY[
    'memory.plan_owner_v5_local_claim_projection_v1(integer)'::regprocedure,
    'memory.register_owner_v5_local_claim_projection_v1(uuid,uuid,uuid,uuid,text,text,text,text)'::regprocedure
  ] LOOP
    definition:=pg_get_functiondef(target);
    IF position(new_list IN definition)>0 THEN
      EXECUTE replace(definition,new_list,old_list);
    ELSIF position(old_list IN definition)=0 THEN
      RAISE EXCEPTION 'local claim projection definition drifted: %',target;
    END IF;
  END LOOP;
END
$restore_functions$;

CREATE OR REPLACE FUNCTION memory.render_claim_projection_text_v5_1(
  p_subject_entity_type text,
  p_subject_canonical_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_canonical_name text,
  p_object_literal jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  literal_value text;
  result_value text;
BEGIN
  IF p_subject_entity_type IS NULL
     OR btrim(COALESCE(p_subject_canonical_name,''))=''
     OR length(p_subject_canonical_name)>500
     OR p_subject_canonical_name ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'subject entity is unsafe for deterministic rendering'
      USING ERRCODE='23514';
  END IF;
  CASE p_predicate
    WHEN 'relationship.has_pet' THEN
      IF p_object_kind<>'entity'
         OR p_subject_entity_type NOT IN ('self','person')
         OR p_object_entity_type<>'animal'
         OR p_object_literal IS NOT NULL
         OR btrim(COALESCE(p_object_canonical_name,''))=''
         OR length(p_object_canonical_name)>500
         OR p_object_canonical_name ~ '[[:cntrl:]]' THEN
        RAISE EXCEPTION 'pet relationship is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      IF p_subject_entity_type='self' THEN
        result_value:=format('The user has a pet named %s.',p_object_canonical_name);
      ELSE
        result_value:=format('%s has a pet named %s.',
          p_subject_canonical_name,p_object_canonical_name);
      END IF;
    WHEN 'identity.name' THEN
      IF p_object_kind<>'literal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'name observation is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=CASE p_subject_entity_type
        WHEN 'self' THEN format('The user''s name is %s.',literal_value)
        WHEN 'person' THEN format('This person''s name is %s.',literal_value)
        WHEN 'animal' THEN format('This animal''s name is %s.',literal_value)
        WHEN 'organization' THEN format('This organization''s name is %s.',literal_value)
        WHEN 'place' THEN format('This place''s name is %s.',literal_value)
        WHEN 'project' THEN format('This project''s name is %s.',literal_value)
        WHEN 'object' THEN format('This object''s name is %s.',literal_value)
        WHEN 'concept' THEN format('This concept''s name is %s.',literal_value)
        ELSE NULL
      END;
      IF result_value IS NULL THEN
        RAISE EXCEPTION 'name subject type is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
    WHEN 'pet.breed' THEN
      IF p_object_kind<>'literal'
         OR p_subject_entity_type<>'animal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'pet breed is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=format('%s has recorded breed %s.',
        p_subject_canonical_name,literal_value);
    WHEN 'pet.sex' THEN
      IF p_object_kind<>'literal'
         OR p_subject_entity_type<>'animal'
         OR p_object_entity_type IS NOT NULL
         OR p_object_canonical_name IS NOT NULL THEN
        RAISE EXCEPTION 'pet sex is outside the rendering contract'
          USING ERRCODE='23514';
      END IF;
      literal_value:=memory.v5_claim_literal_value_v5_1(
        p_predicate,p_object_literal
      );
      result_value:=format('%s has recorded sex %s.',
        p_subject_canonical_name,literal_value);
    ELSE
      RAISE EXCEPTION 'predicate has no deterministic V5.1 claim renderer'
        USING ERRCODE='23514';
  END CASE;
  IF btrim(result_value)<>result_value
     OR result_value=''
     OR length(result_value)>2000
     OR result_value ~ '[[:cntrl:]]' THEN
    RAISE EXCEPTION 'rendered claim text is invalid'
      USING ERRCODE='23514';
  END IF;
  RETURN result_value;
END
$function$;

ALTER FUNCTION memory.render_claim_projection_text_v5_1(
  text,text,text,text,text,text,jsonb
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_claim_projection_source_v5_1(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.plan_owner_v5_local_claim_projection_v1(integer)
  OWNER TO memory_v5_local_projection_maintainer;
ALTER FUNCTION memory.register_owner_v5_local_claim_projection_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_local_projection_maintainer;

COMMENT ON FUNCTION memory.render_claim_projection_text_v5_1(
  text,text,text,text,text,text,jsonb
) IS NULL;

COMMIT;
