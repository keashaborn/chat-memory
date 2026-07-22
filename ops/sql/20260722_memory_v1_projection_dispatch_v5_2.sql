BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regclass('memory.projection_plan') IS NULL
     OR to_regclass('memory.projection_plan_item') IS NULL
     OR to_regclass('memory.projection_claim_payload') IS NULL
     OR to_regclass('memory.claim') IS NULL
     OR to_regclass('memory.preference_head_v5') IS NULL
     OR to_regclass('memory.project_knowledge_head_v5') IS NULL
     OR EXISTS (
       SELECT 1
       FROM pg_class AS relation
       JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
       WHERE namespace.nspname='memory'
         AND relation.relname IN (
           'claim','preference_head_v5','project_knowledge_head_v5'
         )
         AND (NOT relation.relrowsecurity OR NOT relation.relforcerowsecurity)
     )
     OR NOT EXISTS (
       SELECT 1 FROM pg_roles
       WHERE rolname='memory_v5_writer' AND NOT rolcanlogin
         AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
         AND NOT rolinherit AND NOT rolbypassrls
     ) THEN
    RAISE EXCEPTION 'V5.2 projection dispatch prerequisites are missing';
  END IF;
END
$prerequisite$;

ALTER TABLE memory.projection_plan
  DROP CONSTRAINT IF EXISTS projection_plan_predicate_registry_version_check;
ALTER TABLE memory.projection_plan
  ADD CONSTRAINT projection_plan_predicate_registry_version_check CHECK (
    predicate_registry_version IN (
      'memory_predicate_registry_v5',
      'memory_predicate_registry_v5_1',
      'memory_predicate_registry_v5_2'
    )
  );

ALTER TABLE memory.projection_plan_item
  DROP CONSTRAINT IF EXISTS projection_plan_item_predicate_registry_version_check;
ALTER TABLE memory.projection_plan_item
  ADD CONSTRAINT projection_plan_item_predicate_registry_version_check CHECK (
    predicate_registry_version IN (
      'memory_predicate_registry_v5',
      'memory_predicate_registry_v5_1',
      'memory_predicate_registry_v5_2'
    )
  );

ALTER TABLE memory.projection_claim_payload
  DROP CONSTRAINT IF EXISTS projection_claim_payload_claim_class_check;
ALTER TABLE memory.projection_claim_payload
  ADD CONSTRAINT projection_claim_payload_claim_class_check CHECK (
    claim_class IN (
      'direct_claim','supportive_context','correction',
      'reported_stance','never_surface'
    )
  );

CREATE OR REPLACE FUNCTION memory.v5_2_projection_sentence(
  p_subject text,
  p_affirmative text,
  p_negative text,
  p_polarity memory.observation_polarity,
  p_modality memory.observation_modality
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
  SELECT p_subject || ' ' || CASE
    WHEN p_polarity='negated' THEN p_negative
    WHEN p_modality='uncertain' THEN 'may ' || p_affirmative
    ELSE p_affirmative
  END || '.'
$function$;

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
    AND (binding.object_entity_id IS NULL OR object_entity.status='active');
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
      WHEN 'relationship.healthcare_provider_for' THEN 'a healthcare provider for'
      WHEN 'relationship.in_law_of' THEN 'an in-law of'
      WHEN 'relationship.manager_of' THEN 'a manager of'
      WHEN 'relationship.mentor_of' THEN 'a mentor of'
      WHEN 'relationship.neighbor_of' THEN 'a neighbor of'
      WHEN 'relationship.parent_of' THEN 'a parent of'
      WHEN 'relationship.plan_helper_for' THEN 'a plan helper for'
      WHEN 'relationship.relative_of' THEN 'a relative of'
      WHEN 'relationship.romantic_partner_of' THEN 'a romantic partner of'
      WHEN 'relationship.roommate_of' THEN 'a roommate of'
      WHEN 'relationship.sibling_of' THEN 'a sibling of'
      WHEN 'relationship.spouse_of' THEN 'a spouse of'
      WHEN 'relationship.teacher_of' THEN 'a teacher of'
      WHEN 'relationship.teammate_of' THEN 'a teammate of'
      WHEN 'relationship.training_partner_of' THEN 'a training partner of'
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
        ('social.experiences_tension_with','experiences tension with','does not experience tension with'),
        ('social.feels_close_to','feels close to','does not feel close to'),
        ('social.feels_unsafe_with','feels unsafe with','does not feel unsafe with'),
        ('social.in_conflict_with','is in conflict with','is not in conflict with'),
        ('social.no_contact_with','has no contact with','is not out of contact with'),
        ('social.perceives_as_adversary','perceives as an adversary','does not perceive as an adversary'),
        ('social.supports','supports','does not support'),
        ('social.trusts','trusts','does not trust')
      ) AS pair(predicate,affirmative,negative)
      WHERE pair.predicate=source.predicate;
      IF social_verb IS NULL THEN
        RAISE EXCEPTION 'unsupported V5.2 social renderer';
      END IF;
      IF source.modality='reported_observation' THEN
        result_value := subject_label||' reports that '||subject_label||' '||
          CASE WHEN source.polarity='negated' THEN social_negative ELSE social_verb END||
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
        subject_label,'attended '||object_label,'did not attend '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='employment.worked_for' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'worked for '||object_label,'did not work for '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='occupation.works_as' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'works as '||object_label,'does not work as '||object_label,
        source.polarity,source.modality
      );
    ELSIF source.predicate='residence.lives_at' THEN
      result_value := memory.v5_2_projection_sentence(
        subject_label,'lives at '||object_label,'does not live at '||object_label,
        source.polarity,source.modality
      );
    ELSE
      RAISE EXCEPTION 'unsupported V5.2 entity renderer: %',source.predicate;
    END IF;
  ELSE
    value_text := source.object_literal->>'value';
    unit_text := source.object_literal->>'unit';
    approximate_prefix := CASE
      WHEN (source.object_literal->>'approximate')::boolean THEN 'approximately '
      ELSE '' END;
    IF source.predicate IN ('identity.name','identity.name_canonical') THEN
      result_value := CASE
        WHEN subject_label='The user' THEN 'The user''s '
        WHEN right(subject_label,1)='s' THEN subject_label||''' '
        ELSE subject_label||'''s ' END ||
        CASE source.predicate WHEN 'identity.name_canonical' THEN 'canonical name' ELSE 'name' END ||
        ' is '||value_text||'.';
    ELSIF source.predicate='age.reported' THEN
      result_value := subject_label||' reported an age of '||approximate_prefix||
        value_text||' '||unit_text||'.';
    ELSIF source.predicate='credential.reported' THEN
      result_value := subject_label||' reported the credential '||value_text||'.';
    ELSIF source.predicate='health.user_reported_observation' THEN
      result_value := 'The user reported'||CASE WHEN subject_label='The user' THEN '' ELSE ' about '||subject_label END||': '||value_text||'.';
    ELSIF source.predicate='health.user_reported_uncertain_label' THEN
      result_value := 'The user reported an uncertain health label'||CASE WHEN subject_label='The user' THEN '' ELSE ' about '||subject_label END||': '||value_text||'.';
    ELSIF source.predicate='life_event.died' THEN
      IF source.object_literal->'value'<>'true'::jsonb THEN
        RAISE EXCEPTION 'death observation must use literal true';
      END IF;
      result_value := subject_label||CASE WHEN source.modality='uncertain' THEN ' may have died.' ELSE ' died.' END;
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
      result_value := subject_label||' was reported to weigh '||approximate_prefix||
        value_text||' '||unit_text||'.';
    ELSIF source.predicate='stance.reported' THEN
      result_value := subject_label||' reports the position that '||
        source.object_literal#>>'{value,position}'||'.';
    ELSE
      RAISE EXCEPTION 'unsupported V5.2 literal renderer: %',source.predicate;
    END IF;
  END IF;
  IF result_value IS NULL OR btrim(result_value)='' OR length(result_value)>2000 THEN
    RAISE EXCEPTION 'V5.2 claim renderer produced invalid text';
  END IF;
  RETURN result_value;
END
$function$;

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
    observation.owner_user_id,observation.observation_id,
    observation.observation_sha256,observation.evidence_id,
    evidence.content_sha256,evidence.status::text,
    observation.predicate_registry_version,observation.predicate,
    observation.polarity::text,observation.modality::text,
    observation.projection_class::text,observation.surface_policy::text,
    observation.sensitivity::text,observation.extraction_confidence::text,
    binding.subject_entity_id,subject_entity.entity_type,
    subject_entity.status::text,subject_entity.canonical_name,
    CASE WHEN binding.object_entity_id IS NULL THEN 'literal' ELSE 'entity' END,
    binding.object_entity_id,object_entity.entity_type,object_entity.status::text,
    object_entity.canonical_name,observation.object_literal,
    CASE WHEN observation.object_literal IS NULL THEN NULL ELSE
      memory.v5_digest_text(memory.v5_canonical_json_text(observation.object_literal))
    END,
    CASE WHEN observation.project_scope->>'state'='resolved' THEN
      observation.project_scope||jsonb_build_object('project_id',project.project_id::text)
    ELSE observation.project_scope END,
    jsonb_build_object(
      'semantic',temporal.semantic::text,'shape',temporal.shape::text,
      'basis',temporal.basis::text,'source_form',temporal.source_form::text,
      'certainty',temporal.certainty::text,'precision',temporal.precision::text,
      'normalized_sha256',temporal.normalized_sha256
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
  JOIN memory.predicate_contract AS contract
    ON contract.predicate=observation.predicate
   AND contract.registry_version=observation.predicate_registry_version
  LEFT JOIN memory.project_space AS project
    ON project.owner_user_id=observation.owner_user_id
   AND project.project_key=observation.project_scope->>'project_key'
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.predicate_registry_version='memory_predicate_registry_v5_2'
    AND evidence.status='active'
    AND subject_entity.status='active'
    AND (binding.object_entity_id IS NULL OR object_entity.status='active')
    AND contract.lifecycle='active' AND contract.extraction_allowed
    AND contract.contract->'modalities'?observation.modality::text
    AND contract.contract->'projection_classes'?observation.projection_class::text
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

CREATE OR REPLACE FUNCTION memory.expected_projection_payload_v5_2(
  p_observation_id uuid
)
RETURNS TABLE(
  lane memory.projection_lane_v5,
  lane_scope jsonb,
  payload jsonb,
  temporal_materialization memory.projection_temporal_materialization_v5,
  temporal_source_observation_id uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  source record;
  value jsonb;
  domain text;
  target text;
  context_value jsonb;
  dimension text;
  knowledge_kind text;
  project_id_value uuid;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_observation_id);
  value := source.object_literal->'value';
  IF source.projection_class IN (
    'direct_claim','supportive_context','correction',
    'reported_stance','never_surface'
  ) THEN
    lane := 'claim';
    lane_scope := '{}'::jsonb;
    payload := jsonb_build_object(
      'kind','claim','claim_class',source.projection_class,
      'canonical_text',memory.render_projection_claim_text_v5_2(p_observation_id),
      'surface_policy',source.surface_policy
    );
    temporal_materialization := 'link_only';
    temporal_source_observation_id := NULL;
  ELSIF source.projection_class='life_preference' THEN
    domain := value->>'domain';
    target := value->>'target';
    context_value := value->'context';
    lane := 'preference';
    lane_scope := jsonb_build_object(
      'preference_class','life','domain',domain,
      'preference_key','life.'||domain||'.'||substr(
        encode(digest(convert_to(lower(target),'UTF8'),'sha256'),'hex'),1,24
      ),'scope','user_global'
    );
    payload := jsonb_build_object(
      'kind','preference','preference_class','life','domain',domain,
      'preference_key',lane_scope->>'preference_key',
      'value',jsonb_build_object('target',target,'context',context_value),
      'preference_polarity',value->>'polarity','scope','user_global',
      'stability',CASE WHEN source.modality='uncertain' THEN 'tentative'
        WHEN context_value<>'null'::jsonb THEN 'contextual' ELSE 'stable' END,
      'surface_policy',source.surface_policy
    );
    temporal_materialization := 'link_only';
    temporal_source_observation_id := NULL;
  ELSIF source.projection_class='response_preference' THEN
    dimension := value->>'dimension';
    lane := 'preference';
    lane_scope := jsonb_build_object(
      'preference_class','response','domain','response',
      'preference_key','response.'||dimension,'scope','user_global'
    );
    payload := jsonb_build_object(
      'kind','preference','preference_class','response','domain','response',
      'preference_key','response.'||dimension,'value',value->>'value',
      'preference_polarity','not_applicable','scope','user_global',
      'stability','stable','surface_policy',source.surface_policy
    );
    temporal_materialization := 'link_only';
    temporal_source_observation_id := NULL;
  ELSIF source.projection_class='project_knowledge' THEN
    knowledge_kind := CASE source.predicate
      WHEN 'project.constraint' THEN 'constraint'
      WHEN 'project.current_state' THEN 'current_state'
      WHEN 'project.proposed_feature' THEN 'proposed_feature'
      WHEN 'project.requirement' THEN 'requirement' ELSE NULL END;
    project_id_value := (source.project_scope->>'project_id')::uuid;
    IF knowledge_kind IS NULL OR project_id_value IS NULL THEN
      RAISE EXCEPTION 'resolved V5.2 project projection is incomplete';
    END IF;
    lane := 'project_knowledge';
    lane_scope := jsonb_build_object(
      'project_id',project_id_value::text,
      'component_key',source.project_scope->'component_key',
      'binding_source',source.project_scope->>'binding_source',
      'knowledge_kind',knowledge_kind,
      'knowledge_key',knowledge_kind||'.'||substr(
        encode(digest(convert_to(lower(value#>>'{}'),'UTF8'),'sha256'),'hex'),1,24
      )
    );
    payload := jsonb_build_object(
      'kind','project_knowledge','project_id',project_id_value::text,
      'component_key',source.project_scope->'component_key',
      'binding_source',source.project_scope->>'binding_source',
      'knowledge_kind',knowledge_kind,
      'knowledge_key',lane_scope->>'knowledge_key',
      'canonical_text',value#>>'{}',
      'document_state',CASE WHEN knowledge_kind='proposed_feature' THEN 'proposed' ELSE 'working' END,
      'authority_level','user_reported','surface_policy',source.surface_policy
    );
    temporal_materialization := CASE
      WHEN source.predicate='project.current_state'
       AND source.temporal->>'semantic'='state_validity'
       AND source.temporal->>'basis' IN ('instant','calendar')
      THEN 'state_validity_only'::memory.projection_temporal_materialization_v5
      ELSE 'link_only'::memory.projection_temporal_materialization_v5 END;
    temporal_source_observation_id := CASE
      WHEN temporal_materialization='state_validity_only' THEN p_observation_id
      ELSE NULL END;
  ELSE
    RAISE EXCEPTION 'V5.2 projection class is outside durable lanes';
  END IF;
  RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_packet_v5_2(
  p_plan_id uuid,
  p_packet_text text
)
RETURNS TABLE(
  packet_text_sha256 text,
  semantic_key_sha256 text,
  projection_sha256 text,
  packet_sha256 text,
  owner_manifest_sha256 text,
  existing_aggregates integer,
  existing_plans integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection jsonb;
  identity jsonb;
  input jsonb;
  source record;
  expected record;
  semantic_value text;
  projection_hash text;
  packet_hash text;
  aggregate_count integer;
  plan_count integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR btrim(COALESCE(p_packet_text,''))='' THEN
    RAISE EXCEPTION 'V5.2 projection identifiers are required';
  END IF;
  BEGIN packet := p_packet_text::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'V5.2 projection packet is not valid JSON';
  END;
  IF NOT memory.v5_jsonb_exact_keys(packet,ARRAY[
       'contract_version','predicate_registry_version','projection_policy_version',
       'projector','projector_version','projections','packet_sha256'
     ])
     OR packet->>'contract_version'<>'memory_v1_projection_plan_v5'
     OR packet->>'predicate_registry_version'<>'memory_predicate_registry_v5_2'
     OR packet->>'projection_policy_version'<>'memory_projection_policy_v5'
     OR packet->>'projector'<>'memory_v1_deterministic_projection_v5_2'
     OR packet->>'projector_version'<>'semantic_dispatch_v1'
     OR jsonb_typeof(packet->'projections')<>'array'
     OR jsonb_array_length(packet->'projections')<>1 THEN
    RAISE EXCEPTION 'V5.2 projection envelope mismatch';
  END IF;
  projection := packet->'projections'->0;
  identity := projection->'identity';
  input := projection->'observation_inputs'->0;
  IF NOT memory.v5_jsonb_exact_keys(projection,ARRAY[
       'projection_ref','lane','observation_inputs','identity','target',
       'temporal_policy','review','relations','payload'
     ])
     OR NOT memory.v5_jsonb_exact_keys(identity,ARRAY[
       'subject_entity_id','predicate','object_kind','object_entity_id',
       'object_literal_sha256','polarity','modality','semantic_key_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(input,ARRAY[
       'observation_id','observation_sha256','stance'
     ])
     OR projection->>'projection_ref'<>'p01'
     OR jsonb_array_length(projection->'observation_inputs')<>1
     OR input->>'stance'<>'supports'
     OR projection#>>'{target,action}'<>'create'
     OR projection#>'{target,aggregate_id}'<>'null'::jsonb
     OR projection#>'{target,expected_revision_number}'<>'null'::jsonb
     OR projection#>'{target,reason_codes}'<>'[]'::jsonb
     OR projection#>>'{review,state}'<>'manual_review_required'
     OR (projection#>>'{review,authorization_required}')::boolean IS NOT TRUE
     OR projection#>'{review,reason_codes}'<>'["initial_v5_2_projection_requires_review"]'::jsonb
     OR projection->'relations'<>'[]'::jsonb THEN
    RAISE EXCEPTION 'V5.2 projection body mismatch';
  END IF;
  SELECT * INTO STRICT source FROM memory.preflight_projection_source_v5_2(
    (input->>'observation_id')::uuid
  );
  SELECT * INTO STRICT expected FROM memory.expected_projection_payload_v5_2(
    source.observation_id
  );
  IF input->>'observation_sha256'<>source.observation_sha256
     OR projection->>'lane'<>expected.lane::text
     OR projection->'payload'<>expected.payload
     OR identity->>'subject_entity_id'<>source.subject_entity_id::text
     OR identity->>'predicate'<>source.predicate
     OR identity->>'object_kind'<>source.object_kind
     OR NULLIF(identity->>'object_entity_id','') IS DISTINCT FROM source.object_entity_id::text
     OR NULLIF(identity->>'object_literal_sha256','') IS DISTINCT FROM source.object_literal_sha256
     OR identity->>'polarity'<>source.polarity
     OR identity->>'modality'<>source.modality
     OR projection#>>'{temporal_policy,canonical_source}'<>'memory.observation_temporal'
     OR projection#>>'{temporal_policy,materialization}'<>expected.temporal_materialization::text
     OR NULLIF(projection#>>'{temporal_policy,source_observation_id}','')
          IS DISTINCT FROM expected.temporal_source_observation_id::text THEN
    RAISE EXCEPTION 'V5.2 projection is not an exact deterministic source projection';
  END IF;
  semantic_value := memory.v5_projection_semantic_key_sha256(
    actor,expected.lane,source.subject_entity_id,source.predicate,
    source.object_kind,source.object_entity_id,source.object_literal_sha256,
    source.polarity::memory.observation_polarity,
    source.modality::memory.observation_modality,expected.lane_scope
  );
  projection_hash := memory.v5_digest_text(memory.v5_canonical_json_text(projection));
  packet_hash := memory.v5_digest_text(memory.v5_canonical_json_text(packet-'packet_sha256'));
  IF identity->>'semantic_key_sha256'<>semantic_value
     OR packet->>'packet_sha256'<>packet_hash THEN
    RAISE EXCEPTION 'V5.2 projection hash mismatch';
  END IF;
  IF expected.lane='claim' THEN
    SELECT count(*) INTO aggregate_count FROM memory.claim AS aggregate
    WHERE aggregate.owner_user_id=actor
      AND aggregate.canonical_key='v5:'||semantic_value;
  ELSIF expected.lane='preference' THEN
    SELECT count(*) INTO aggregate_count FROM memory.preference_head_v5 AS aggregate
    WHERE aggregate.owner_user_id=actor
      AND aggregate.semantic_key_sha256=semantic_value;
  ELSE
    SELECT count(*) INTO aggregate_count FROM memory.project_knowledge_head_v5 AS aggregate
    WHERE aggregate.owner_user_id=actor
      AND aggregate.semantic_key_sha256=semantic_value;
  END IF;
  SELECT count(*) INTO plan_count FROM memory.projection_plan AS stored_plan
  WHERE stored_plan.owner_user_id=actor
    AND (stored_plan.plan_id=p_plan_id OR stored_plan.packet_sha256=packet_hash);
  RETURN QUERY SELECT
    memory.v5_digest_text(p_packet_text),semantic_value,projection_hash,
    packet_hash,memory.v5_projection_owner_manifest_sha256(actor,packet_hash),
    aggregate_count,plan_count;
END
$function$;

CREATE OR REPLACE FUNCTION memory.stage_projection_plan_v5_2(
  p_plan_id uuid,
  p_packet_text text,
  p_expected_owner_manifest_sha256 text
)
RETURNS TABLE(plan_id uuid,outcome text,rows_written integer,result jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet jsonb;
  projection jsonb;
  identity jsonb;
  payload jsonb;
  input jsonb;
  expected record;
  preflight record;
  existing memory.projection_plan%ROWTYPE;
  result_value jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_plan_id IS NULL OR NOT memory.v5_sha256_valid(p_expected_owner_manifest_sha256) THEN
    RAISE EXCEPTION 'V5.2 projection stage identifiers are invalid';
  END IF;
  packet := p_packet_text::jsonb;
  projection := packet->'projections'->0;
  identity := projection->'identity';
  payload := projection->'payload';
  input := projection->'observation_inputs'->0;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|projection-v5-2|'||p_plan_id::text,0
  ));
  SELECT stored.* INTO existing FROM memory.projection_plan AS stored
  WHERE stored.owner_user_id=actor
    AND (stored.plan_id=p_plan_id OR stored.packet_sha256=packet->>'packet_sha256');
  IF FOUND THEN
    IF existing.plan_id<>p_plan_id OR existing.packet_text<>p_packet_text
       OR existing.owner_manifest_sha256<>p_expected_owner_manifest_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_item AS item
         WHERE item.owner_user_id=actor AND item.plan_id=p_plan_id
           AND item.projection_ref='p01' AND item.projection=projection
       )
       OR NOT EXISTS (
         SELECT 1 FROM memory.projection_plan_observation AS link
         WHERE link.owner_user_id=actor AND link.plan_id=p_plan_id
           AND link.projection_ref='p01'
           AND link.observation_id=(input->>'observation_id')::uuid
           AND link.observation_sha256=input->>'observation_sha256'
       ) THEN
      RAISE EXCEPTION 'V5.2 projection replay state mismatch';
    END IF;
    RETURN QUERY SELECT p_plan_id,'replayed',0,jsonb_build_object(
      'plan_id',p_plan_id,'projection_ref','p01','packet_sha256',existing.packet_sha256
    );
    RETURN;
  END IF;
  SELECT * INTO STRICT preflight FROM memory.preflight_projection_packet_v5_2(
    p_plan_id,p_packet_text
  );
  IF preflight.existing_aggregates<>0 OR preflight.existing_plans<>0
     OR preflight.owner_manifest_sha256<>p_expected_owner_manifest_sha256 THEN
    RAISE EXCEPTION 'V5.2 projection preflight is stale or mismatched';
  END IF;
  SELECT * INTO STRICT expected FROM memory.expected_projection_payload_v5_2(
    (input->>'observation_id')::uuid
  );
  INSERT INTO memory.projection_plan(
    owner_user_id,plan_id,contract_version,predicate_registry_version,
    projection_policy_version,projector,projector_version,packet_text,
    packet_text_sha256,packet_sha256,owner_manifest_sha256,
    projection_count,invoked_by_session
  ) VALUES (
    actor,p_plan_id,packet->>'contract_version',packet->>'predicate_registry_version',
    packet->>'projection_policy_version',packet->>'projector',packet->>'projector_version',
    p_packet_text,preflight.packet_text_sha256,preflight.packet_sha256,
    preflight.owner_manifest_sha256,1,session_user
  );
  INSERT INTO memory.projection_plan_item(
    owner_user_id,plan_id,projection_ref,predicate_registry_version,lane,
    projection,projection_sha256,subject_entity_id,predicate,object_kind,
    object_entity_id,object_literal_sha256,polarity,modality,lane_scope,
    semantic_key_sha256,target_action,expected_revision_number,
    target_reason_codes,temporal_materialization,temporal_source_observation_id,
    review_state,authorization_required,review_reason_codes
  ) VALUES (
    actor,p_plan_id,'p01','memory_predicate_registry_v5_2',expected.lane,
    projection,preflight.projection_sha256,(identity->>'subject_entity_id')::uuid,
    identity->>'predicate',identity->>'object_kind',
    NULLIF(identity->>'object_entity_id','')::uuid,
    NULLIF(identity->>'object_literal_sha256',''),
    (identity->>'polarity')::memory.observation_polarity,
    (identity->>'modality')::memory.observation_modality,
    expected.lane_scope,preflight.semantic_key_sha256,'create',NULL,'[]'::jsonb,
    expected.temporal_materialization,expected.temporal_source_observation_id,
    'manual_review_required',true,projection#>'{review,reason_codes}'
  );
  IF expected.lane='claim' THEN
    INSERT INTO memory.projection_claim_payload(
      owner_user_id,plan_id,projection_ref,target_action,target_claim_id,
      claim_class,canonical_text,surface_policy
    ) VALUES (
      actor,p_plan_id,'p01','create',NULL,payload->>'claim_class',
      payload->>'canonical_text',
      (payload->>'surface_policy')::memory.observation_surface_policy
    );
  ELSIF expected.lane='preference' THEN
    INSERT INTO memory.projection_preference_payload(
      owner_user_id,plan_id,projection_ref,target_action,target_preference_id,
      preference_class,preference_domain,preference_key,value,
      preference_polarity,scope,stability,surface_policy
    ) VALUES (
      actor,p_plan_id,'p01','create',NULL,payload->>'preference_class',
      payload->>'domain',payload->>'preference_key',payload->'value',
      payload->>'preference_polarity',payload->>'scope',payload->>'stability',
      (payload->>'surface_policy')::memory.observation_surface_policy
    );
  ELSE
    INSERT INTO memory.projection_project_payload(
      owner_user_id,plan_id,projection_ref,target_action,project_id,
      component_key,binding_source,target_knowledge_id,knowledge_kind,
      knowledge_key,canonical_text,document_state,authority_level,surface_policy
    ) VALUES (
      actor,p_plan_id,'p01','create',(payload->>'project_id')::uuid,
      NULLIF(payload->>'component_key',''),payload->>'binding_source',NULL,
      payload->>'knowledge_kind',payload->>'knowledge_key',payload->>'canonical_text',
      payload->>'document_state',payload->>'authority_level',
      (payload->>'surface_policy')::memory.observation_surface_policy
    );
  END IF;
  INSERT INTO memory.projection_plan_observation(
    owner_user_id,plan_id,projection_ref,observation_id,observation_sha256,stance
  ) VALUES (
    actor,p_plan_id,'p01',(input->>'observation_id')::uuid,
    input->>'observation_sha256','supports'
  );
  SET CONSTRAINTS ALL IMMEDIATE;
  result_value := jsonb_build_object(
    'plan_id',p_plan_id,'projection_ref','p01',
    'packet_sha256',preflight.packet_sha256,'lane',expected.lane::text
  );
  RETURN QUERY SELECT p_plan_id,'applied',4,result_value;
END
$function$;

ALTER FUNCTION memory.v5_2_projection_sentence(
  text,text,text,memory.observation_polarity,memory.observation_modality
) OWNER TO memory_v5_writer;
ALTER FUNCTION memory.render_projection_claim_text_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_source_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.expected_projection_payload_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.preflight_projection_packet_v5_2(uuid,text)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.stage_projection_plan_v5_2(uuid,text,text)
  OWNER TO memory_v5_writer;

-- These are the same owner-filtered reads required by the existing V5 apply
-- path.  FORCE RLS remains authoritative for the NOLOGIN writer role.
GRANT SELECT ON
  memory.claim,
  memory.preference_head_v5,
  memory.project_knowledge_head_v5
TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.v5_2_projection_sentence(
  text,text,text,memory.observation_polarity,memory.observation_modality
) FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.render_projection_claim_text_v5_2(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.expected_projection_payload_v5_2(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.preflight_projection_packet_v5_2(uuid,text)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.stage_projection_plan_v5_2(uuid,text,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.preflight_projection_source_v5_2(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_projection_packet_v5_2(uuid,text)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.stage_projection_plan_v5_2(uuid,text,text)
  TO brains_app;

COMMIT;
