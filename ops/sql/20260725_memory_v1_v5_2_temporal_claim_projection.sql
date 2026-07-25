BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.preflight_projection_source_v5_2(uuid)') IS NULL
     OR to_regprocedure('memory.render_projection_claim_text_v5_2(uuid)') IS NULL
     OR to_regprocedure('memory.expected_projection_payload_v5_2(uuid)') IS NULL
     OR to_regclass('memory.observation_temporal') IS NULL THEN
    RAISE EXCEPTION 'V5.2 temporal claim projection prerequisites are absent';
  END IF;
END
$block$;

CREATE OR REPLACE FUNCTION memory.preflight_projection_temporal_state_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  relation_value text;
BEGIN
  actor := memory.require_v5_writer_context();
  SELECT CASE
    WHEN temporal.semantic <> 'state_validity' THEN 'not_applicable'
    WHEN temporal.basis = 'instant' AND temporal.instant_range IS NOT NULL
      AND NOT upper_inf(temporal.instant_range) THEN 'historical'
    WHEN temporal.basis = 'calendar' AND temporal.calendar_range IS NOT NULL
      AND NOT upper_inf(temporal.calendar_range) THEN 'historical'
    WHEN temporal.basis = 'instant' AND temporal.instant_range IS NOT NULL
      AND upper_inf(temporal.instant_range) THEN 'current'
    WHEN temporal.basis = 'calendar' AND temporal.calendar_range IS NOT NULL
      AND upper_inf(temporal.calendar_range) THEN 'current'
    WHEN temporal.instant_at IS NOT NULL THEN 'point'
    ELSE 'unknown'
  END
  INTO STRICT relation_value
  FROM memory.observation AS observation
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  WHERE observation.owner_user_id=actor
    AND observation.observation_id=p_observation_id
    AND observation.predicate_registry_version='memory_predicate_registry_v5_2'
    AND evidence.status='active';
  RETURN relation_value;
EXCEPTION
  WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
    RAISE EXCEPTION 'exact owner-scoped temporal projection source not found'
      USING ERRCODE='P0002';
END
$function$;

CREATE OR REPLACE FUNCTION memory.render_projection_claim_text_temporal_v5_2(
  p_observation_id uuid
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  source record;
  subject_label text;
  object_label text;
  state_relation text;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(p_observation_id);
  state_relation := memory.preflight_projection_temporal_state_v5_2(
    p_observation_id
  );
  IF source.predicate <> 'occupation.works_as'
     OR state_relation <> 'historical' THEN
    RETURN memory.render_projection_claim_text_v5_2(p_observation_id);
  END IF;
  subject_label := CASE source.subject_entity_type
    WHEN 'self' THEN 'The user' ELSE source.subject_canonical_name END;
  object_label := CASE source.object_entity_type
    WHEN 'self' THEN 'The user' ELSE source.object_canonical_name END;
  IF source.object_kind <> 'entity'
     OR object_label IS NULL
     OR btrim(object_label)='' THEN
    RAISE EXCEPTION 'historical occupation projection requires an entity object';
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
      'canonical_text',
        memory.render_projection_claim_text_temporal_v5_2(p_observation_id),
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
      'document_state',CASE WHEN knowledge_kind='proposed_feature'
        THEN 'proposed' ELSE 'working' END,
      'authority_level','user_reported',
      'surface_policy',source.surface_policy
    );
    temporal_materialization := CASE
      WHEN source.predicate='project.current_state'
       AND source.temporal->>'semantic'='state_validity'
       AND source.temporal->>'basis' IN ('instant','calendar')
      THEN 'state_validity_only'::memory.projection_temporal_materialization_v5
      ELSE 'link_only'::memory.projection_temporal_materialization_v5 END;
    temporal_source_observation_id := CASE
      WHEN temporal_materialization='state_validity_only'
      THEN p_observation_id ELSE NULL END;
  ELSE
    RAISE EXCEPTION 'V5.2 projection class is outside durable lanes';
  END IF;
  RETURN NEXT;
END
$function$;

ALTER FUNCTION memory.preflight_projection_temporal_state_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.render_projection_claim_text_temporal_v5_2(uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.expected_projection_payload_v5_2(uuid)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION
  memory.preflight_projection_temporal_state_v5_2(uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.render_projection_claim_text_temporal_v5_2(uuid)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.preflight_projection_temporal_state_v5_2(uuid)
  TO brains_app;

COMMIT;
