\set ON_ERROR_STOP on
BEGIN;

CREATE OR REPLACE FUNCTION memory.preflight_projection_source_v5_2(
  p_observation_id uuid
)
RETURNS TABLE(
  owner_user_id uuid,
  observation_id uuid,
  observation_sha256 text,
  evidence_id uuid,
  evidence_content_sha256 text,
  evidence_status text,
  predicate_registry_version text,
  predicate text,
  polarity text,
  modality text,
  projection_class text,
  surface_policy text,
  sensitivity text,
  extraction_confidence text,
  subject_entity_id uuid,
  subject_entity_type text,
  subject_entity_status text,
  subject_canonical_name text,
  object_kind text,
  object_entity_id uuid,
  object_entity_type text,
  object_entity_status text,
  object_canonical_name text,
  object_literal jsonb,
  object_literal_sha256 text,
  project_scope jsonb,
  temporal jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched integer;
BEGIN
  actor := memory.require_v5_writer_context();
  RETURN QUERY
  SELECT
    observation.owner_user_id,
    observation.observation_id,
    observation.observation_sha256,
    observation.evidence_id,
    evidence.content_sha256,
    evidence.status::text,
    CASE
      WHEN compatibility.canonical_name_correction
      THEN 'memory_predicate_registry_v5_2'
      ELSE observation.predicate_registry_version
    END,
    observation.predicate,
    observation.polarity::text,
    observation.modality::text,
    CASE
      WHEN compatibility.canonical_name_correction THEN 'direct_claim'
      ELSE observation.projection_class::text
    END,
    observation.surface_policy::text,
    observation.sensitivity::text,
    observation.extraction_confidence::text,
    binding.subject_entity_id,
    subject_entity.entity_type,
    subject_entity.status::text,
    subject_entity.canonical_name,
    CASE
      WHEN binding.object_entity_id IS NULL THEN 'literal'
      ELSE 'entity'
    END,
    binding.object_entity_id,
    object_entity.entity_type,
    object_entity.status::text,
    object_entity.canonical_name,
    CASE
      WHEN compatibility.canonical_name_correction
      THEN jsonb_set(
        observation.object_literal,
        '{value}',
        to_jsonb(subject_entity.canonical_name),
        false
      )
      ELSE observation.object_literal
    END,
    CASE
      WHEN observation.object_literal IS NULL THEN NULL
      WHEN compatibility.canonical_name_correction THEN
        memory.v5_digest_text(memory.v5_canonical_json_text(jsonb_set(
          observation.object_literal,
          '{value}',
          to_jsonb(subject_entity.canonical_name),
          false
        )))
      ELSE memory.v5_digest_text(
        memory.v5_canonical_json_text(observation.object_literal)
      )
    END,
    CASE
      WHEN observation.project_scope->>'state'='resolved'
      THEN observation.project_scope || jsonb_build_object(
        'project_id', project.project_id::text
      )
      ELSE observation.project_scope
    END,
    jsonb_build_object(
      'semantic', temporal.semantic::text,
      'shape', temporal.shape::text,
      'basis', temporal.basis::text,
      'source_form', temporal.source_form::text,
      'certainty', temporal.certainty::text,
      'precision', temporal.precision::text,
      'normalized_sha256', temporal.normalized_sha256
    )
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  CROSS JOIN LATERAL (
    SELECT EXISTS (
      SELECT 1
      FROM memory.entity_correction_target_reconciliation_v5_2
        AS reconciliation
      WHERE reconciliation.owner_user_id=observation.owner_user_id
        AND reconciliation.observation_id=observation.observation_id
        AND reconciliation.successor_resolution_id=
          binding.subject_resolution_id
        AND reconciliation.target_entity_id=binding.subject_entity_id
        AND observation.predicate_registry_version=
          'memory_predicate_registry_v5'
        AND observation.predicate='identity.name_canonical'
        AND observation.modality='corrective'
        AND observation.polarity='affirmed'
        AND observation.projection_class='correction'
        AND observation.surface_policy='direct_or_relevant'
        AND observation.object_literal->>'kind'='literal'
        AND observation.object_literal->>'datatype'='text'
        AND lower(btrim(observation.object_literal->>'value'))=
          lower(btrim(subject_entity.canonical_name))
    ) AS canonical_name_correction
  ) AS compatibility
  JOIN memory.predicate_contract AS contract
    ON contract.predicate=observation.predicate
   AND contract.registry_version=CASE
     WHEN compatibility.canonical_name_correction
     THEN 'memory_predicate_registry_v5_2'
     ELSE observation.predicate_registry_version
   END
  LEFT JOIN memory.project_space AS project
    ON project.owner_user_id=observation.owner_user_id
   AND project.project_key=observation.project_scope->>'project_key'
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND (
      observation.predicate_registry_version=
        'memory_predicate_registry_v5_2'
      OR compatibility.canonical_name_correction
    )
    AND evidence.status='active'
    AND subject_entity.status='active'
    AND (
      binding.object_entity_id IS NULL
      OR object_entity.status='active'
    )
    AND contract.lifecycle='active'
    AND contract.extraction_allowed
    AND contract.contract->'modalities'?observation.modality::text
    AND contract.contract->'projection_classes'?(
      CASE
        WHEN compatibility.canonical_name_correction THEN 'direct_claim'
        ELSE observation.projection_class::text
      END
    )
    AND contract.contract->'surface_policies'?observation.surface_policy::text
    AND (
      observation.project_scope->>'state'<>'resolved'
      OR project.project_id IS NOT NULL
    );
  GET DIAGNOSTICS matched=ROW_COUNT;
  IF matched<>1 THEN
    RAISE EXCEPTION 'complete owner-scoped V5.2 projection source not found'
      USING ERRCODE='P0002';
  END IF;
END
$function$;

ALTER FUNCTION memory.preflight_projection_source_v5_2(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  TO brains_app;

CREATE OR REPLACE FUNCTION memory.render_projection_claim_text_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  source record;
  subject_label text;
  object_label text;
  relation_phrase text;
  social_verb text;
  social_negative text;
  value_text text;
  unit_text text;
  approximate_prefix text;
  result_value text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT
    observation.*,binding.subject_entity_id,binding.object_entity_id,
    subject_entity.entity_type AS subject_entity_type,
    subject_entity.canonical_name AS subject_canonical_name,
    object_entity.entity_type AS object_entity_type,
    object_entity.canonical_name AS object_canonical_name
  INTO source
  FROM memory.observation AS observation
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=binding.owner_user_id
   AND subject_entity.entity_id=binding.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=binding.owner_user_id
   AND object_entity.entity_id=binding.object_entity_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.predicate_registry_version='memory_predicate_registry_v5_2'
    AND evidence.status='active'
    AND subject_entity.status='active'
    AND (
      binding.object_entity_id IS NULL
      OR object_entity.status='active'
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'complete owner-scoped V5.2 claim source not found'
      USING ERRCODE='P0002';
  END IF;
  subject_label := CASE source.subject_entity_type
    WHEN 'self' THEN 'The user' ELSE source.subject_canonical_name END;
  IF source.object_entity_id IS NOT NULL THEN
    object_label := CASE source.object_entity_type
      WHEN 'self' THEN 'The user' ELSE source.object_canonical_name END;
    relation_phrase := CASE source.predicate
      WHEN 'relationship.acquaintance_of' THEN 'an acquaintance of'
      WHEN 'relationship.aunt_or_uncle_of' THEN 'an aunt or uncle of'
      WHEN 'relationship.business_partner_of' THEN 'a business partner of'
      WHEN 'relationship.caregiver_for' THEN 'a caregiver for'
      WHEN 'relationship.coach_of' THEN 'a coach of'
      WHEN 'relationship.collaborator_with' THEN 'a collaborator with'
      WHEN 'relationship.cousin_of' THEN 'a cousin of'
      WHEN 'relationship.coworker_of' THEN 'a coworker of'
      WHEN 'relationship.friend_of' THEN 'a friend of'
      WHEN 'relationship.grandparent_of' THEN 'a grandparent of'
      WHEN 'relationship.guardian_of' THEN 'a guardian of'
      WHEN 'relationship.healthcare_provider_for' THEN
        'a healthcare provider for'
      WHEN 'relationship.in_law_of' THEN 'an in-law of'
      WHEN 'relationship.manager_of' THEN 'a manager of'
      WHEN 'relationship.mentor_of' THEN 'a mentor of'
      WHEN 'relationship.neighbor_of' THEN 'a neighbor of'
      WHEN 'relationship.parent_of' THEN 'a parent of'
      WHEN 'relationship.plan_helper_for' THEN 'a plan helper for'
      WHEN 'relationship.relative_of' THEN 'a relative of'
      WHEN 'relationship.romantic_partner_of' THEN
        'a romantic partner of'
      WHEN 'relationship.roommate_of' THEN 'a roommate of'
      WHEN 'relationship.sibling_of' THEN 'a sibling of'
      WHEN 'relationship.spouse_of' THEN 'a spouse of'
      WHEN 'relationship.teacher_of' THEN 'a teacher of'
      WHEN 'relationship.teammate_of' THEN 'a teammate of'
      WHEN 'relationship.training_partner_of' THEN
        'a training partner of'
      ELSE NULL END;
    IF source.predicate='relationship.has_pet' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'has a pet named '||object_label,
        'does not have a pet named '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='relationship.lives_with' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'lives with '||object_label,
        'does not live with '||object_label,
        source.polarity,source.modality
      );
    ELSIF relation_phrase IS NOT NULL THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'is '||relation_phrase||' '||object_label,
        'is not '||relation_phrase||' '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate LIKE 'social.%' THEN
      SELECT pair.affirmative,pair.negative
      INTO social_verb,social_negative
      FROM (VALUES
        ('social.avoids','avoids','does not avoid'),
        ('social.competes_with','competes with','does not compete with'),
        ('social.depends_on','depends on','does not depend on'),
        ('social.distrusts','distrusts','does not distrust'),
        ('social.estranged_from','is estranged from','is not estranged from'),
        ('social.experiences_tension_with','experiences tension with',
          'does not experience tension with'),
        ('social.feels_close_to','feels close to','does not feel close to'),
        ('social.feels_unsafe_with','feels unsafe with',
          'does not feel unsafe with'),
        ('social.in_conflict_with','is in conflict with',
          'is not in conflict with'),
        ('social.no_contact_with','has no contact with',
          'is not out of contact with'),
        ('social.perceives_as_adversary','perceives as an adversary',
          'does not perceive as an adversary'),
        ('social.supports','supports','does not support'),
        ('social.trusts','trusts','does not trust')
      ) AS pair(predicate,affirmative,negative)
      WHERE pair.predicate=source.predicate;
      IF social_verb IS NULL THEN
        RAISE EXCEPTION 'unsupported V5.2 social renderer';
      END IF;
      IF source.modality='reported_observation' THEN
        result_value := subject_label||' reports that '||subject_label||' '||
          CASE WHEN source.polarity='negated'
            THEN social_negative ELSE social_verb END||
          ' '||object_label||'.';
      ELSE
        result_value := memory.v5_2_projection_sentence(
          subject_label,social_verb||' '||object_label,
          social_negative||' '||object_label,
          source.polarity,source.modality
        );
      END IF;
    ELSIF source.predicate='education.attended' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'attended '||object_label,
        'did not attend '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='employment.worked_for' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'worked for '||object_label,
        'did not work for '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='occupation.works_as' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'works as '||object_label,
        'does not work as '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='residence.lives_at' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'lives at '||object_label,
        'does not live at '||object_label,
        source.polarity,source.modality
      );
    ELSE
      RAISE EXCEPTION
        'unsupported V5.2 entity renderer: %',source.predicate;
    END IF;
  ELSE
    value_text := source.object_literal->>'value';
    unit_text := source.object_literal->>'unit';
    approximate_prefix := CASE
      WHEN (source.object_literal->>'approximate')::boolean
      THEN 'approximately '
      ELSE '' END;
    IF source.predicate IN ('identity.name','identity.name_canonical') THEN
      result_value := CASE
        WHEN subject_label='The user' THEN 'The user''s '
        WHEN right(subject_label,1)='s' THEN subject_label||''' '
        ELSE subject_label||'''s ' END ||
        CASE source.predicate
          WHEN 'identity.name_canonical' THEN 'canonical name'
          ELSE 'name' END ||
        ' is '||value_text||'.';
    ELSIF source.predicate='age.reported' THEN
      result_value := subject_label||' reported an age of '||
        approximate_prefix||value_text||' '||unit_text||'.';
    ELSIF source.predicate='credential.reported' THEN
      result_value :=
        subject_label||' reported the credential '||value_text||'.';
    ELSIF source.predicate='health.user_reported_observation' THEN
      result_value := 'The user reported'||
        CASE WHEN subject_label='The user'
          THEN '' ELSE ' about '||subject_label END||
        ': '||value_text||'.';
    ELSIF source.predicate='health.user_reported_uncertain_label' THEN
      result_value := 'The user reported an uncertain health label'||
        CASE WHEN subject_label='The user'
          THEN '' ELSE ' about '||subject_label END||
        ': '||value_text||'.';
    ELSIF source.predicate='life_event.died' THEN
      IF source.object_literal->'value'<>'true'::jsonb THEN
        RAISE EXCEPTION 'death observation must use literal true';
      END IF;
      result_value := subject_label||
        CASE WHEN source.modality='uncertain'
          THEN ' may have died.' ELSE ' died.' END;
    ELSIF source.predicate IN (
      'pet.breed','pet.coat_color','pet.eye_color','pet.hearing_status',
      'pet.sex','pet.species'
    ) THEN
      result_value := subject_label||' has '||CASE source.predicate
        WHEN 'pet.breed' THEN 'recorded breed '
        WHEN 'pet.coat_color' THEN 'recorded coat color '
        WHEN 'pet.eye_color' THEN 'recorded eye color '
        WHEN 'pet.hearing_status' THEN 'recorded hearing status '
        WHEN 'pet.sex' THEN 'recorded sex '
        ELSE 'recorded species ' END||value_text||'.';
    ELSIF source.predicate='pet.weight_reported' THEN
      result_value := subject_label||' was reported to weigh '||
        approximate_prefix||value_text||' '||unit_text||'.';
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports this position: "'||
        regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        )||
        CASE WHEN right(regexp_replace(
          btrim(source.object_literal::jsonb#>>'{value,position}'),
          '[[:space:]]+',' ','g'
        ),1) ~ '[.!?]' THEN '' ELSE '.' END||'"';
    ELSE
      RAISE EXCEPTION
        'unsupported V5.2 literal renderer: %',source.predicate;
    END IF;
  END IF;
  IF result_value IS NULL
     OR btrim(result_value)=''
     OR length(result_value)>2000 THEN
    RAISE EXCEPTION 'V5.2 claim renderer produced invalid text';
  END IF;
  RETURN result_value;
END
$function$;

ALTER FUNCTION memory.render_projection_claim_text_v5_2(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.render_projection_claim_text_v5_2(uuid)
  FROM PUBLIC,brains_app;

DROP FUNCTION IF EXISTS
memory.v5_2_legacy_stage_projection_source_allowed_v1(uuid,uuid,text);

COMMIT;
