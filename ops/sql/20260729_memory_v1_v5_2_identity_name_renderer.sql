\set ON_ERROR_STOP on
BEGIN;

CREATE OR REPLACE FUNCTION memory.render_projection_claim_text_temporal_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  source record;
  subject_label text;
  identity_subject_label text;
  object_label text;
  state_relation text;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_observation_id);
  state_relation := memory.preflight_projection_temporal_state_v5_2(
    p_observation_id
  );
  subject_label := CASE source.subject_entity_type
    WHEN 'self' THEN 'The user'
    ELSE source.subject_canonical_name
  END;

  IF source.predicate IN ('identity.name','identity.name_canonical')
     AND source.object_kind='literal' THEN
    identity_subject_label := CASE source.subject_entity_type
      WHEN 'self' THEN 'The user'
      WHEN 'person' THEN 'This person'
      WHEN 'animal' THEN 'This animal'
      WHEN 'organization' THEN 'This organization'
      WHEN 'place' THEN 'This place'
      WHEN 'project' THEN 'This project'
      WHEN 'object' THEN 'This object'
      WHEN 'concept' THEN 'This concept'
      ELSE NULL
    END;
    IF identity_subject_label IS NULL
       OR source.object_literal->>'value' IS NULL
       OR btrim(source.object_literal->>'value')='' THEN
      RAISE EXCEPTION 'identity-name projection is incomplete';
    END IF;
    RETURN CASE
      WHEN identity_subject_label='The user' THEN 'The user''s '
      ELSE identity_subject_label||'''s '
    END ||
      CASE source.predicate
        WHEN 'identity.name_canonical' THEN 'canonical name'
        ELSE 'name'
      END || ' is ' || (source.object_literal->>'value') || '.';
  END IF;

  IF state_relation <> 'historical'
     OR source.predicate NOT IN (
       'occupation.works_as',
       'relationship.has_pet'
     ) THEN
    RETURN memory.render_projection_claim_text_v5_2(p_observation_id);
  END IF;

  object_label := CASE source.object_entity_type
    WHEN 'self' THEN 'The user'
    ELSE source.object_canonical_name
  END;
  IF source.object_kind <> 'entity'
     OR object_label IS NULL
     OR btrim(object_label)='' THEN
    RAISE EXCEPTION
      'historical entity projection requires an entity object';
  END IF;

  IF source.predicate='relationship.has_pet' THEN
    RETURN subject_label || CASE
      WHEN source.polarity='negated' THEN
        ' did not formerly have a pet named ' || object_label || '.'
      WHEN source.modality='uncertain' THEN
        ' may formerly have had a pet named ' || object_label || '.'
      ELSE
        ' formerly had a pet named ' || object_label || '.'
    END;
  END IF;

  RETURN subject_label || CASE
    WHEN source.polarity='negated' THEN
      ' did not formerly work as ' || object_label || '.'
    WHEN source.modality='uncertain' THEN
      ' may formerly have worked as ' || object_label || '.'
    ELSE
      ' formerly worked as ' || object_label || '.'
  END;
END
$function$;

ALTER FUNCTION memory.render_projection_claim_text_temporal_v5_2(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.render_projection_claim_text_temporal_v5_2(uuid) FROM PUBLIC;

COMMIT;
