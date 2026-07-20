BEGIN;

-- Read-only, owner-scoped input surface for deterministic V5.1 shadow
-- generation.  The application receives identifiers, hashes, policy metadata,
-- and governed link roles only; it never receives evidence or claim prose.

DO $roles$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname='memory_v5_epistemic_writer'
  ) THEN
    RAISE EXCEPTION 'memory_v5_epistemic_writer is required';
  END IF;
END
$roles$;

GRANT SELECT ON memory.observation_temporal,
  memory.claim_observation,
  memory.preference_revision_observation,
  memory.project_knowledge_revision_observation,
  memory.retrieval_outcome_signal_v5_1
TO memory_v5_epistemic_writer;

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.observation_temporal;
CREATE POLICY epistemic_v5_1_reference_read
  ON memory.observation_temporal
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.claim_observation;
CREATE POLICY epistemic_v5_1_reference_read
  ON memory.claim_observation
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.preference_revision_observation;
CREATE POLICY epistemic_v5_1_reference_read
  ON memory.preference_revision_observation
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.project_knowledge_revision_observation;
CREATE POLICY epistemic_v5_1_reference_read
  ON memory.project_knowledge_revision_observation
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS epistemic_v5_1_reference_read
  ON memory.retrieval_outcome_signal_v5_1;
CREATE POLICY epistemic_v5_1_reference_read
  ON memory.retrieval_outcome_signal_v5_1
  FOR SELECT TO memory_v5_epistemic_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.load_pattern_salience_shadow_inputs_v5_1(
  p_max_observations integer DEFAULT 256,
  p_max_targets integer DEFAULT 256
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  observation_total integer;
  target_total integer;
  observation_rows jsonb;
  target_rows jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF p_max_observations NOT BETWEEN 1 AND 256
     OR p_max_targets NOT BETWEEN 1 AND 256 THEN
    RAISE EXCEPTION 'shadow input limits must be between 1 and 256'
      USING ERRCODE='22023';
  END IF;

  SELECT count(*) INTO observation_total
  FROM memory.observation AS observation
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
   AND evidence.status='active'
  WHERE observation.owner_user_id=actor;

  SELECT COALESCE(jsonb_agg(value.row_value ORDER BY value.created_at,
    value.observation_id),'[]'::jsonb)
  INTO observation_rows
  FROM (
    SELECT observation.created_at,observation.observation_id,
      jsonb_build_object(
        'observation_id',observation.observation_id::text,
        'evidence_id',observation.evidence_id::text,
        'predicate',observation.predicate,
        'predicate_registry_version',observation.predicate_registry_version,
        'subject_entity_id',binding.subject_entity_id::text,
        'subject_entity_type',subject_entity.entity_type,
        'object_kind',CASE WHEN observation.object_mention_id IS NULL
          THEN 'literal' ELSE 'entity' END,
        'object_entity_id',binding.object_entity_id::text,
        'object_literal_sha256',CASE
          WHEN observation.object_literal IS NULL THEN NULL
          ELSE memory.v5_digest_text(
            memory.v5_canonical_json_text(observation.object_literal)
          ) END,
        'polarity',observation.polarity::text,
        'modality',observation.modality::text,
        'projection_class',observation.projection_class::text,
        'surface_policy',observation.surface_policy::text,
        'sensitivity',observation.sensitivity::text,
        'extraction_confidence',observation.extraction_confidence,
        'observation_sha256',observation.observation_sha256,
        'evidence_content_sha256',evidence.content_sha256,
        'evidence_directness',evidence.directness,
        'evidence_source_reliability',evidence.source_reliability,
        'evidence_source_system',evidence.source_system,
        'independence_key_sha256',memory.v5_digest_text(COALESCE(
          evidence.independence_key,evidence.content_sha256,
          evidence.evidence_id::text
        )),
        'episode_key_sha256',memory.v5_digest_text(concat_ws('|',
          'memory_v1_episode_v5_1',evidence.source_system,
          CASE WHEN temporal.source_form::text IN (
            'absolute','partial_absolute','relative'
          ) THEN temporal.normalized_sha256
          ELSE COALESCE(
            NULLIF(evidence.metadata->>'thread_id',''),
            evidence.external_id
          ) END
        )),
        'temporal_bucket_sha256',memory.v5_digest_text(concat_ws('|',
          'memory_v1_temporal_bucket_v5_1',
          to_char(date_trunc('week',COALESCE(
            CASE WHEN temporal.basis='instant'
              THEN temporal.instant_at END,
            evidence.observed_at,evidence.recorded_at
          ) AT TIME ZONE 'UTC'),'YYYY-MM-DD')
        )),
        'effective_date',to_char(COALESCE(
          CASE WHEN temporal.basis='instant'
            THEN temporal.instant_at END,
          evidence.observed_at,evidence.recorded_at
        ) AT TIME ZONE 'UTC','YYYY-MM-DD'),
        'temporal_basis',COALESCE(temporal.basis::text,'missing'),
        'temporal_source_form',COALESCE(temporal.source_form::text,'missing')
      ) AS row_value
    FROM memory.observation AS observation
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    LEFT JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=observation.owner_user_id
     AND binding.observation_id=observation.observation_id
    LEFT JOIN memory.entity AS subject_entity
      ON subject_entity.owner_user_id=binding.owner_user_id
     AND subject_entity.entity_id=binding.subject_entity_id
    LEFT JOIN memory.observation_temporal AS temporal
      ON temporal.owner_user_id=observation.owner_user_id
     AND temporal.observation_id=observation.observation_id
    WHERE observation.owner_user_id=actor
    ORDER BY observation.created_at,observation.observation_id
    LIMIT p_max_observations
  ) AS value;

  WITH target_values AS (
    SELECT 'claim'::text AS target_kind,head.claim_id AS target_id,
      revision.revision_number,
      memory.v5_digest_text(head.canonical_key) AS semantic_key_sha256,
      head.status::text AS status,head.sensitivity::text AS sensitivity,
      head.predicate AS target_class,
      CASE WHEN head.object_entity_id IS NULL
        THEN 'literal' ELSE 'entity' END AS object_kind,
      head.valid_from,head.valid_to,head.created_at,
      NULL::text AS authority_state,
      COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
          'observation_id',link.observation_id::text,
          'stance',link.stance::text,'relevance',link.relevance
        ) ORDER BY link.observation_id,link.stance::text)
        FROM memory.claim_observation AS link
        WHERE link.owner_user_id=head.owner_user_id
          AND link.claim_id=head.claim_id
      ),'[]'::jsonb) AS observation_links
    FROM memory.claim AS head
    JOIN LATERAL (
      SELECT value.revision_number
      FROM memory.claim_revision AS value
      WHERE value.owner_user_id=head.owner_user_id
        AND value.claim_id=head.claim_id
      ORDER BY value.revision_number DESC LIMIT 1
    ) AS revision ON true
    WHERE head.owner_user_id=actor

    UNION ALL

    SELECT 'preference'::text,head.preference_id,revision.revision_number,
      head.semantic_key_sha256,head.status::text,
      COALESCE((
        SELECT max(observation.sensitivity::text)
        FROM memory.preference_revision_observation AS link
        JOIN memory.observation AS observation
          ON observation.owner_user_id=link.owner_user_id
         AND observation.observation_id=link.observation_id
        WHERE link.owner_user_id=head.owner_user_id
          AND link.revision_id=revision.revision_id
      ),'medium'),
      concat_ws('.',head.preference_class,head.preference_domain,
        head.preference_key),
      'literal',NULL::timestamptz,NULL::timestamptz,head.created_at,
      revision.stability,
      COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
          'observation_id',link.observation_id::text,
          'stance',link.stance::text,'relevance',link.relevance
        ) ORDER BY link.observation_id,link.stance::text)
        FROM memory.preference_revision_observation AS link
        WHERE link.owner_user_id=head.owner_user_id
          AND link.revision_id=revision.revision_id
      ),'[]'::jsonb)
    FROM memory.preference_head_v5 AS head
    JOIN memory.preference_revision_v5 AS revision
      ON revision.owner_user_id=head.owner_user_id
     AND revision.preference_id=head.preference_id
     AND revision.revision_id=head.current_revision_id
    WHERE head.owner_user_id=actor

    UNION ALL

    SELECT 'project_knowledge'::text,head.knowledge_id,
      revision.revision_number,head.semantic_key_sha256,head.status::text,
      COALESCE((
        SELECT max(observation.sensitivity::text)
        FROM memory.project_knowledge_revision_observation AS link
        JOIN memory.observation AS observation
          ON observation.owner_user_id=link.owner_user_id
         AND observation.observation_id=link.observation_id
        WHERE link.owner_user_id=head.owner_user_id
          AND link.project_id=head.project_id
          AND link.revision_id=revision.revision_id
      ),'medium'),
      concat_ws('.',head.knowledge_kind,head.knowledge_key),
      'literal',NULL::timestamptz,NULL::timestamptz,head.created_at,
      concat_ws(':',revision.document_state,revision.authority_level),
      COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
          'observation_id',link.observation_id::text,
          'stance',link.stance::text,'relevance',link.relevance
        ) ORDER BY link.observation_id,link.stance::text)
        FROM memory.project_knowledge_revision_observation AS link
        WHERE link.owner_user_id=head.owner_user_id
          AND link.project_id=head.project_id
          AND link.revision_id=revision.revision_id
      ),'[]'::jsonb)
    FROM memory.project_knowledge_head_v5 AS head
    JOIN memory.project_knowledge_revision_v5 AS revision
      ON revision.owner_user_id=head.owner_user_id
     AND revision.project_id=head.project_id
     AND revision.knowledge_id=head.knowledge_id
     AND revision.revision_id=head.current_revision_id
    WHERE head.owner_user_id=actor
  ), limited_targets AS (
    SELECT * FROM target_values
    ORDER BY target_kind,target_id
    LIMIT p_max_targets
  )
  SELECT
    (SELECT count(*) FROM target_values),
    COALESCE(jsonb_agg(jsonb_build_object(
      'target_kind',target_kind,
      'target_id',target_id::text,
      'target_revision_number',revision_number,
      'semantic_key_sha256',semantic_key_sha256,
      'status',status,
      'sensitivity',sensitivity,
      'target_class',target_class,
      'object_kind',object_kind,
      'valid_from',CASE WHEN valid_from IS NULL THEN NULL
        ELSE to_char(valid_from AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END,
      'valid_to',CASE WHEN valid_to IS NULL THEN NULL
        ELSE to_char(valid_to AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END,
      'created_at',to_char(created_at AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'authority_state',authority_state,
      'observation_links',observation_links
    ) ORDER BY target_kind,target_id),'[]'::jsonb)
  INTO target_total,target_rows
  FROM limited_targets;

  RETURN jsonb_build_object(
    'contract_version','memory_v1_pattern_salience_shadow_input_v5_1',
    'owner_user_id_sha256',memory.v5_digest_text(actor::text),
    'observation_total',observation_total,
    'observation_returned',jsonb_array_length(observation_rows),
    'observations_truncated',observation_total>p_max_observations,
    'target_total',target_total,
    'target_returned',jsonb_array_length(target_rows),
    'targets_truncated',target_total>p_max_targets,
    'observations',observation_rows,
    'targets',target_rows
  );
END
$function$;

ALTER FUNCTION memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)
  OWNER TO memory_v5_epistemic_writer;
REVOKE ALL ON FUNCTION
  memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)
  FROM PUBLIC,memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION
  memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)
  TO brains_app;

COMMIT;
