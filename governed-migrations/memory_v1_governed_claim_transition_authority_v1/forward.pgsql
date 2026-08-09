GRANT SELECT ON memory.projection_outbox TO memory_v5_writer;

GRANT EXECUTE ON FUNCTION
  memory.v5_sha256_valid(text),
  memory.v5_digest_text(text),
  memory.v5_canonical_json_text(jsonb),
  memory.v5_projection_owner_manifest_sha256(uuid,text),
  memory.v5_projection_semantic_key_sha256(
    uuid,memory.projection_lane_v5,uuid,text,text,uuid,text,
    memory.observation_polarity,memory.observation_modality,jsonb
  ),
  memory.v5_projection_review_manifest_sha256(
    uuid,uuid,text,text,text,integer,memory.projection_review_decision_v5,
    text,text,text,jsonb
  ),
  memory.v5_projection_apply_manifest_sha256(
    uuid,text,uuid,text,text,text,memory.projection_lane_v5,
    memory.projection_target_action_v5,integer,uuid,text
  )
TO brains_app;

CREATE VIEW memory.read_governed_claim_projection_apply_ready_v1
WITH (security_barrier=true, security_invoker=false)
AS
SELECT
  'memory_v1_governed_claim_projection_apply_ready_v1'::text
    AS contract_version,
  plan.owner_user_id,
  plan.plan_id,
  item.projection_ref,
  item.lane::text AS lane,
  item.target_action::text AS target_action,
  item.review_state::text AS review_state,
  0::integer AS current_revision_number,
  review.review_id AS projection_review_id,
  review.review_number AS projection_review_number,
  plan.owner_manifest_sha256,
  item.projection_sha256,
  item.semantic_key_sha256,
  review.authorization_manifest_sha256
    AS projection_review_manifest_sha256,
  memory.v5_projection_apply_manifest_sha256(
    plan.owner_user_id,
    plan.owner_manifest_sha256,
    plan.plan_id,
    item.projection_ref,
    item.projection_sha256,
    item.semantic_key_sha256,
    item.lane,
    item.target_action,
    0,
    review.review_id,
    review.authorization_manifest_sha256
  ) AS projection_apply_manifest_sha256,
  observation_authority.observation_count,
  observation_authority.observation_authority_sha256
FROM memory.projection_plan AS plan
JOIN memory.projection_plan_item AS item
  ON item.owner_user_id=plan.owner_user_id
 AND item.plan_id=plan.plan_id
JOIN memory.projection_claim_payload AS payload
  ON payload.owner_user_id=item.owner_user_id
 AND payload.plan_id=item.plan_id
 AND payload.projection_ref=item.projection_ref
JOIN memory.projection_review AS review
  ON review.owner_user_id=item.owner_user_id
 AND review.plan_id=item.plan_id
 AND review.projection_ref=item.projection_ref
JOIN LATERAL (
  SELECT
    pg_catalog.count(*)::integer AS observation_count,
    pg_catalog.count(*) FILTER (
      WHERE plan_observation.stance='supports'
        AND evidence.status='active'
        AND memory.v5_sha256_valid(evidence.content_sha256)
        AND evidence.content_sha256=memory.v5_digest_text(evidence.content)
        AND memory.v5_sha256_valid(observation.observation_sha256)
        AND memory.v5_sha256_valid(temporal.normalized_sha256)
    )::integer AS valid_observation_count,
    memory.v5_digest_text(memory.v5_canonical_json_text(
      pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
          'observation_id',observation.observation_id::text,
          'observation_sha256',observation.observation_sha256,
          'evidence_id',evidence.evidence_id::text,
          'evidence_content_sha256',evidence.content_sha256,
          'evidence_status',evidence.status::text,
          'stance','supports',
          'relevance','1.000',
          'temporal_sha256',temporal.normalized_sha256
        ) ORDER BY observation.observation_id,'supports'
      )
    )) AS observation_authority_sha256
  FROM memory.projection_plan_observation AS plan_observation
  JOIN memory.observation AS observation
    ON observation.owner_user_id=plan_observation.owner_user_id
   AND observation.observation_id=plan_observation.observation_id
   AND observation.observation_sha256=plan_observation.observation_sha256
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  WHERE plan_observation.owner_user_id=item.owner_user_id
    AND plan_observation.plan_id=item.plan_id
    AND plan_observation.projection_ref=item.projection_ref
) AS observation_authority ON true
WHERE session_user='brains_app'
  AND memory.current_actor_user_id() IS NOT NULL
  AND plan.owner_user_id=(SELECT memory.current_actor_user_id())
  AND plan.contract_version='memory_v1_projection_plan_v5'
  AND plan.predicate_registry_version='memory_predicate_registry_v5_2'
  AND plan.projection_policy_version='memory_projection_policy_v5'
  AND plan.packet_text_sha256=memory.v5_digest_text(plan.packet_text)
  AND plan.packet_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text((plan.packet_text::jsonb)-'packet_sha256')
  )
  AND plan.owner_manifest_sha256=
    memory.v5_projection_owner_manifest_sha256(
      plan.owner_user_id,plan.packet_sha256
    )
  AND plan.projection_count=(
    SELECT pg_catalog.count(*)
    FROM memory.projection_plan_item AS plan_count
    WHERE plan_count.owner_user_id=plan.owner_user_id
      AND plan_count.plan_id=plan.plan_id
  )
  AND item.lane='claim'
  AND item.target_action='create'
  AND item.expected_revision_number IS NULL
  AND item.review_state='manual_review_required'
  AND item.authorization_required
  AND item.projection_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text(item.projection)
  )
  AND item.semantic_key_sha256=
    memory.v5_projection_semantic_key_sha256(
      item.owner_user_id,item.lane,item.subject_entity_id,item.predicate,
      item.object_kind,item.object_entity_id,item.object_literal_sha256,
      item.polarity,item.modality,item.lane_scope
    )
  AND payload.lane='claim'
  AND payload.target_action='create'
  AND payload.target_claim_id IS NULL
  AND review.parent_review_state='manual_review_required'
  AND review.decision='authorized'
  AND review.expected_projection_sha256=item.projection_sha256
  AND review.expected_semantic_key_sha256=item.semantic_key_sha256
  AND review.authorization_manifest_sha256=
    memory.v5_projection_review_manifest_sha256(
      review.owner_user_id,review.plan_id,review.projection_ref,
      review.expected_projection_sha256,
      review.expected_semantic_key_sha256,review.review_number,
      review.decision,review.reviewer_type,review.reviewer_ref,
      review.reason,review.reason_codes
    )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.projection_review AS later_review
    WHERE later_review.owner_user_id=review.owner_user_id
      AND later_review.plan_id=review.plan_id
      AND later_review.projection_ref=review.projection_ref
      AND later_review.review_number>review.review_number
  )
  AND observation_authority.observation_count BETWEEN 1 AND 100
  AND observation_authority.valid_observation_count=
      observation_authority.observation_count
  AND observation_authority.observation_count=
    pg_catalog.jsonb_array_length(item.projection->'observation_inputs')
  AND observation_authority.observation_count=(
    SELECT pg_catalog.count(*)
    FROM memory.projection_plan_observation AS exact_count
    WHERE exact_count.owner_user_id=item.owner_user_id
      AND exact_count.plan_id=item.plan_id
      AND exact_count.projection_ref=item.projection_ref
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id=item.owner_user_id
      AND relation.plan_id=item.plan_id
      AND relation.projection_ref=item.projection_ref
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.projection_apply_event AS applied
    WHERE applied.owner_user_id=item.owner_user_id
      AND applied.plan_id=item.plan_id
      AND applied.projection_ref=item.projection_ref
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.claim AS existing_claim
    WHERE existing_claim.owner_user_id=item.owner_user_id
      AND existing_claim.canonical_key='v5:'||item.semantic_key_sha256
  );

ALTER TABLE memory.read_governed_claim_projection_apply_ready_v1
  OWNER TO memory_v5_writer;
GRANT SELECT ON memory.read_governed_claim_projection_apply_ready_v1
  TO brains_app;

CREATE VIEW memory.read_governed_claim_materialization_authority_v1
WITH (security_barrier=true, security_invoker=false)
AS
SELECT
  'memory_v1_governed_claim_materialization_authority_v1'::text
    AS contract_version,
  claim.owner_user_id,
  claim.claim_id,
  claim.status::text AS claim_status,
  current_revision.current_revision_number,
  apply_event.request_id AS projection_request_id,
  apply_event.plan_id,
  apply_event.projection_ref,
  review.review_id AS projection_review_id,
  review.review_number AS projection_review_number,
  review.authorization_manifest_sha256
    AS projection_review_manifest_sha256,
  apply_event.event_id AS projection_apply_event_id,
  apply_event.apply_manifest_sha256 AS projection_apply_manifest_sha256,
  candidate_revision.revision_id AS candidate_revision_id,
  candidate_revision.revision_number AS candidate_revision_number,
  memory.v5_digest_text(memory.v5_canonical_json_text(
    candidate_revision.snapshot
  )) AS candidate_claim_state_sha256,
  observation_authority.observation_count,
  observation_authority.observation_authority_sha256
FROM memory.projection_apply_event AS apply_event
JOIN memory.projection_plan AS plan
  ON plan.owner_user_id=apply_event.owner_user_id
 AND plan.plan_id=apply_event.plan_id
JOIN memory.projection_plan_item AS item
  ON item.owner_user_id=apply_event.owner_user_id
 AND item.plan_id=apply_event.plan_id
 AND item.projection_ref=apply_event.projection_ref
JOIN memory.projection_claim_payload AS payload
  ON payload.owner_user_id=item.owner_user_id
 AND payload.plan_id=item.plan_id
 AND payload.projection_ref=item.projection_ref
JOIN memory.projection_review AS review
  ON review.owner_user_id=apply_event.owner_user_id
 AND review.plan_id=apply_event.plan_id
 AND review.projection_ref=apply_event.projection_ref
 AND review.review_id=apply_event.review_id
JOIN memory.claim AS claim
  ON claim.owner_user_id=apply_event.owner_user_id
 AND claim.claim_id=apply_event.resulting_claim_id
JOIN memory.claim_revision AS candidate_revision
  ON candidate_revision.owner_user_id=apply_event.owner_user_id
 AND candidate_revision.claim_id=apply_event.resulting_claim_id
 AND candidate_revision.revision_number=1
JOIN memory.projection_dispatch_v5 AS dispatch
  ON dispatch.owner_user_id=apply_event.owner_user_id
 AND dispatch.apply_event_id=apply_event.event_id
JOIN LATERAL (
  SELECT
    pg_catalog.count(*)::integer AS observation_count,
    pg_catalog.count(*) FILTER (
      WHERE plan_observation.stance='supports'
        AND claim_observation.stance='supports'
        AND claim_observation.relevance=1.000
        AND claim_observation.rationale='memory_projection_v5'
        AND evidence.status='active'
        AND memory.v5_sha256_valid(evidence.content_sha256)
        AND evidence.content_sha256=memory.v5_digest_text(evidence.content)
        AND memory.v5_sha256_valid(observation.observation_sha256)
        AND memory.v5_sha256_valid(temporal.normalized_sha256)
    )::integer AS valid_observation_count,
    memory.v5_digest_text(memory.v5_canonical_json_text(
      pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
          'observation_id',observation.observation_id::text,
          'observation_sha256',observation.observation_sha256,
          'evidence_id',evidence.evidence_id::text,
          'evidence_content_sha256',evidence.content_sha256,
          'evidence_status',evidence.status::text,
          'stance',claim_observation.stance::text,
          'relevance',claim_observation.relevance::text,
          'temporal_sha256',temporal.normalized_sha256
        ) ORDER BY observation.observation_id,claim_observation.stance::text
      )
    )) AS observation_authority_sha256
  FROM memory.projection_plan_observation AS plan_observation
  JOIN memory.observation AS observation
    ON observation.owner_user_id=plan_observation.owner_user_id
   AND observation.observation_id=plan_observation.observation_id
   AND observation.observation_sha256=plan_observation.observation_sha256
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=observation.owner_user_id
   AND evidence.evidence_id=observation.evidence_id
  JOIN memory.observation_temporal AS temporal
    ON temporal.owner_user_id=observation.owner_user_id
   AND temporal.observation_id=observation.observation_id
  JOIN memory.claim_observation AS claim_observation
    ON claim_observation.owner_user_id=plan_observation.owner_user_id
   AND claim_observation.claim_id=apply_event.resulting_claim_id
   AND claim_observation.observation_id=plan_observation.observation_id
  WHERE plan_observation.owner_user_id=apply_event.owner_user_id
    AND plan_observation.plan_id=apply_event.plan_id
    AND plan_observation.projection_ref=apply_event.projection_ref
) AS observation_authority ON true
JOIN LATERAL (
  SELECT
    pg_catalog.max(revision.revision_number)::integer
      AS current_revision_number,
    pg_catalog.count(*)::integer AS revision_count
  FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id=claim.owner_user_id
    AND revision.claim_id=claim.claim_id
) AS current_revision ON true
WHERE session_user='brains_app'
  AND memory.current_actor_user_id() IS NOT NULL
  AND claim.owner_user_id=(SELECT memory.current_actor_user_id())
  AND plan.contract_version='memory_v1_projection_plan_v5'
  AND plan.predicate_registry_version='memory_predicate_registry_v5_2'
  AND plan.projection_policy_version='memory_projection_policy_v5'
  AND plan.owner_manifest_sha256=
    memory.v5_projection_owner_manifest_sha256(
      plan.owner_user_id,plan.packet_sha256
    )
  AND plan.packet_text_sha256=memory.v5_digest_text(plan.packet_text)
  AND plan.packet_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text((plan.packet_text::jsonb)-'packet_sha256')
  )
  AND plan.projection_count=(
    SELECT pg_catalog.count(*)
    FROM memory.projection_plan_item AS plan_count
    WHERE plan_count.owner_user_id=plan.owner_user_id
      AND plan_count.plan_id=plan.plan_id
  )
  AND item.lane='claim'
  AND item.target_action='create'
  AND item.expected_revision_number IS NULL
  AND item.review_state='manual_review_required'
  AND item.authorization_required
  AND item.projection_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text(item.projection)
  )
  AND item.semantic_key_sha256=
    memory.v5_projection_semantic_key_sha256(
      item.owner_user_id,item.lane,item.subject_entity_id,item.predicate,
      item.object_kind,item.object_entity_id,item.object_literal_sha256,
      item.polarity,item.modality,item.lane_scope
    )
  AND payload.lane='claim'
  AND payload.target_action='create'
  AND payload.target_claim_id IS NULL
  AND review.parent_review_state='manual_review_required'
  AND review.decision='authorized'
  AND review.expected_projection_sha256=item.projection_sha256
  AND review.expected_semantic_key_sha256=item.semantic_key_sha256
  AND review.authorization_manifest_sha256=
    memory.v5_projection_review_manifest_sha256(
      review.owner_user_id,review.plan_id,review.projection_ref,
      review.expected_projection_sha256,
      review.expected_semantic_key_sha256,review.review_number,
      review.decision,review.reviewer_type,review.reviewer_ref,
      review.reason,review.reason_codes
    )
  AND NOT EXISTS (
    SELECT 1 FROM memory.projection_review AS later_review
    WHERE later_review.owner_user_id=review.owner_user_id
      AND later_review.plan_id=review.plan_id
      AND later_review.projection_ref=review.projection_ref
      AND later_review.review_number>review.review_number
  )
  AND apply_event.lane='claim'
  AND apply_event.parent_review_state='manual_review_required'
  AND apply_event.review_decision='authorized'
  AND apply_event.outcome='applied'
  AND apply_event.actor_user_id=apply_event.owner_user_id
  AND apply_event.invoked_by_session='brains_app'
  AND apply_event.resulting_claim_revision_number=1
  AND apply_event.apply_manifest_sha256=
    memory.v5_projection_apply_manifest_sha256(
      apply_event.owner_user_id,plan.owner_manifest_sha256,
      apply_event.plan_id,apply_event.projection_ref,
      item.projection_sha256,item.semantic_key_sha256,
      item.lane,item.target_action,0,review.review_id,
      review.authorization_manifest_sha256
    )
  AND candidate_revision.reason='projection_v5_create'
  AND candidate_revision.actor_type='system'
  AND candidate_revision.actor_ref=apply_event.apply_manifest_sha256
  AND candidate_revision.snapshot->>'owner_user_id'=claim.owner_user_id::text
  AND candidate_revision.snapshot->>'claim_id'=claim.claim_id::text
  AND candidate_revision.snapshot->>'status'='candidate'
  AND candidate_revision.snapshot->>'canonical_key'=
      'v5:'||item.semantic_key_sha256
  AND candidate_revision.snapshot->'metadata'=pg_catalog.jsonb_build_object(
    'memory_contract','memory_projection_v5',
    'semantic_key_sha256',item.semantic_key_sha256,
    'projection_sha256',item.projection_sha256
  )
  AND candidate_revision.snapshot->'retrieval_policy'=
    pg_catalog.jsonb_build_object(
      'surface_policy',payload.surface_policy::text,
      'projection_class',payload.claim_class
    )
  AND candidate_revision.snapshot-
        ARRAY['status','confidence','last_confirmed_at','updated_at']::text[]
      =pg_catalog.to_jsonb(claim)-
        ARRAY['status','confidence','last_confirmed_at','updated_at']::text[]
  AND claim.subject_entity_id=item.subject_entity_id
  AND claim.predicate=item.predicate
  AND claim.object_entity_id IS NOT DISTINCT FROM item.object_entity_id
  AND claim.canonical_text=payload.canonical_text
  AND claim.canonical_key='v5:'||item.semantic_key_sha256
  AND claim.qualifiers='{}'::jsonb
  AND claim.retrieval_policy=pg_catalog.jsonb_build_object(
    'surface_policy',payload.surface_policy::text,
    'projection_class',payload.claim_class
  )
  AND claim.metadata=pg_catalog.jsonb_build_object(
    'memory_contract','memory_projection_v5',
    'semantic_key_sha256',item.semantic_key_sha256,
    'projection_sha256',item.projection_sha256
  )
  AND (
    (item.object_kind='entity' AND claim.object_literal IS NULL)
    OR
    (item.object_kind='literal' AND claim.object_literal IS NOT DISTINCT FROM (
      SELECT source_observation.object_literal
      FROM memory.projection_plan_observation AS source_link
      JOIN memory.observation AS source_observation
        ON source_observation.owner_user_id=source_link.owner_user_id
       AND source_observation.observation_id=source_link.observation_id
      WHERE source_link.owner_user_id=item.owner_user_id
        AND source_link.plan_id=item.plan_id
        AND source_link.projection_ref=item.projection_ref
        AND source_link.stance='supports'
      ORDER BY source_link.observation_id
      LIMIT 1
    ))
  )
  AND CASE claim.sensitivity
        WHEN 'restricted' THEN 4 WHEN 'high' THEN 3
        WHEN 'medium' THEN 2 ELSE 1 END
      =(
        SELECT pg_catalog.max(CASE source_observation.sensitivity
          WHEN 'restricted' THEN 4 WHEN 'high' THEN 3
          WHEN 'medium' THEN 2 ELSE 1 END)
        FROM memory.projection_plan_observation AS source_link
        JOIN memory.observation AS source_observation
          ON source_observation.owner_user_id=source_link.owner_user_id
         AND source_observation.observation_id=source_link.observation_id
        WHERE source_link.owner_user_id=item.owner_user_id
          AND source_link.plan_id=item.plan_id
          AND source_link.projection_ref=item.projection_ref
      )
  AND dispatch.lane='claim'
  AND dispatch.operation='upsert'
  AND dispatch.resulting_claim_id=claim.claim_id
  AND dispatch.resulting_claim_revision_number=1
  AND dispatch.resulting_preference_id IS NULL
  AND dispatch.resulting_preference_revision_id IS NULL
  AND dispatch.resulting_project_id IS NULL
  AND dispatch.resulting_knowledge_id IS NULL
  AND dispatch.resulting_project_revision_id IS NULL
  AND dispatch.payload=pg_catalog.jsonb_build_object(
    'contract_version','memory_projection_dispatch_v5',
    'operation','upsert',
    'owner_user_id',claim.owner_user_id,
    'apply_manifest_sha256',apply_event.apply_manifest_sha256,
    'result',pg_catalog.jsonb_build_object(
      'apply_event_id',apply_event.event_id,
      'lane',apply_event.lane,
      'target_action',item.target_action,
      'aggregate_id',claim.claim_id,
      'revision_id',candidate_revision.revision_id,
      'revision_number',1,
      'project_id',NULL::uuid,
      'component_key',NULL::text,
      'binding_source',NULL::text
    )
  )
  AND dispatch.payload_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text(dispatch.payload)
  )
  AND observation_authority.observation_count BETWEEN 1 AND 100
  AND observation_authority.valid_observation_count=
      observation_authority.observation_count
  AND observation_authority.observation_count=(
    SELECT pg_catalog.count(*)
    FROM memory.projection_plan_observation AS plan_count
    WHERE plan_count.owner_user_id=item.owner_user_id
      AND plan_count.plan_id=item.plan_id
      AND plan_count.projection_ref=item.projection_ref
  )
  AND observation_authority.observation_count=(
    SELECT pg_catalog.count(*)
    FROM memory.claim_observation AS claim_count
    WHERE claim_count.owner_user_id=claim.owner_user_id
      AND claim_count.claim_id=claim.claim_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory.projection_plan_relation AS relation
    WHERE relation.owner_user_id=item.owner_user_id
      AND relation.plan_id=item.plan_id
      AND relation.projection_ref=item.projection_ref
  )
  AND (
    (claim.status='candidate'
     AND current_revision.revision_count=1
     AND current_revision.current_revision_number=1
     AND candidate_revision.snapshot=pg_catalog.to_jsonb(claim))
    OR
    (claim.status='supported'
     AND current_revision.revision_count=2
     AND current_revision.current_revision_number=2)
  );

ALTER TABLE memory.read_governed_claim_materialization_authority_v1
  OWNER TO memory_v5_writer;
GRANT SELECT ON memory.read_governed_claim_materialization_authority_v1
  TO brains_app;

CREATE VIEW memory.read_governed_claim_assessment_apply_ready_v1
WITH (security_barrier=true, security_invoker=false)
AS
SELECT
  'memory_v1_governed_claim_assessment_apply_ready_v1'::text
    AS contract_version,
  materialized.owner_user_id,
  materialized.claim_id,
  claim.status::text AS claim_status,
  materialized.current_revision_number AS claim_revision_number,
  materialized.projection_request_id,
  materialized.plan_id,
  materialized.projection_ref,
  materialized.projection_review_id,
  materialized.projection_review_manifest_sha256,
  materialized.projection_apply_event_id,
  materialized.projection_apply_manifest_sha256,
  materialized.candidate_revision_id,
  materialized.candidate_revision_number,
  materialized.candidate_claim_state_sha256,
  materialized.observation_count,
  materialized.observation_authority_sha256,
  review_request.request_id AS assessment_review_request_id,
  review.review_id AS assessment_review_id,
  review.reviewer_type,
  review.reviewer_ref,
  review.action::text AS action,
  review.expected_from_status::text AS expected_from_status,
  review.target_status::text AS target_status,
  review.authorization_manifest_sha256
    AS assessment_review_manifest_sha256,
  memory.v5_digest_text(pg_catalog.concat_ws('|',
    'memory_v1_claim_assessment_apply_v5',
    materialized.owner_user_id::text,
    materialized.claim_id::text,
    review.review_id::text,
    review.authorization_manifest_sha256,
    review.expected_from_status::text,
    review.target_status::text,
    materialized.candidate_revision_number::text,
    materialized.candidate_claim_state_sha256,
    materialized.observation_authority_sha256
  )) AS assessment_apply_manifest_sha256
FROM memory.read_governed_claim_materialization_authority_v1 AS materialized
JOIN memory.claim AS claim
  ON claim.owner_user_id=materialized.owner_user_id
 AND claim.claim_id=materialized.claim_id
JOIN memory.claim_assessment_review_v5 AS review
  ON review.owner_user_id=materialized.owner_user_id
 AND review.claim_id=materialized.claim_id
JOIN memory.relational_operation_request AS review_request
  ON review_request.owner_user_id=review.owner_user_id
 AND review_request.operation='review_claim_assessment_v5'
 AND review_request.target_key=review.claim_id::text
 AND review_request.manifest_sha256=review.authorization_manifest_sha256
WHERE session_user='brains_app'
  AND materialized.owner_user_id=(SELECT memory.current_actor_user_id())
  AND materialized.claim_status='candidate'
  AND materialized.current_revision_number=1
  AND materialized.candidate_revision_number=1
  AND materialized.candidate_claim_state_sha256=memory.v5_digest_text(
    memory.v5_canonical_json_text(pg_catalog.to_jsonb(claim))
  )
  AND review.action='promote_supported'
  AND review.expected_from_status='candidate'
  AND review.target_status='supported'
  AND review.expected_revision_number=1
  AND review.expected_claim_state_sha256=
      materialized.candidate_claim_state_sha256
  AND review.expected_evidence_manifest_sha256=
      materialized.observation_authority_sha256
  AND review.support_score>=0.500
  AND review.claim_confidence>=0.500
  AND review.opposition_score<=review.support_score
  AND review.reviewer_type='user'
  AND review.reviewer_ref=materialized.owner_user_id::text
  AND review.authorization_manifest_sha256=memory.v5_digest_text(
    pg_catalog.concat_ws('|',
      'memory_v1_claim_assessment_review_v5',
      materialized.owner_user_id::text,
      materialized.claim_id::text,
      review.action::text,
      review.expected_from_status::text,
      review.target_status::text,
      review.expected_revision_number::text,
      materialized.candidate_claim_state_sha256,
      materialized.observation_authority_sha256,
      materialized.projection_apply_event_id::text,
      materialized.projection_apply_manifest_sha256,
      review.support_score::text,
      review.opposition_score::text,
      review.claim_confidence::text,
      review.assessment_confidence::text,
      memory.v5_canonical_json_text(review.reason_codes),
      pg_catalog.btrim(review.rationale),
      review.reviewer_type,
      review.reviewer_ref
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.claim_assessment_review_v5 AS later_review
    WHERE later_review.owner_user_id=review.owner_user_id
      AND later_review.claim_id=review.claim_id
      AND (
        later_review.created_at>review.created_at
        OR (later_review.created_at=review.created_at
            AND later_review.review_id>review.review_id)
      )
  )
  AND review_request.request_id<>materialized.projection_request_id
  AND review_request.outcome='applied'
  AND review_request.invoked_by_session='brains_app'
  AND review_request.result=pg_catalog.jsonb_build_object(
    'review_id',review.review_id,
    'claim_id',review.claim_id,
    'target_status',review.target_status
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory.claim_assessment_apply_v5 AS applied
    WHERE applied.owner_user_id=review.owner_user_id
      AND applied.claim_id=review.claim_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory.claim_assessment AS assessment
    WHERE assessment.owner_user_id=review.owner_user_id
      AND assessment.claim_id=review.claim_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM memory.projection_outbox AS outbox
    WHERE outbox.owner_user_id=review.owner_user_id
      AND outbox.aggregate_type='claim'
      AND outbox.aggregate_id=review.claim_id
      AND outbox.operation='upsert'
  );

ALTER TABLE memory.read_governed_claim_assessment_apply_ready_v1
  OWNER TO memory_v5_writer;
GRANT SELECT ON memory.read_governed_claim_assessment_apply_ready_v1
  TO brains_app;

CREATE VIEW memory.read_governed_claim_supported_held_v1
WITH (security_barrier=true, security_invoker=false)
AS
SELECT
  'memory_v1_governed_claim_supported_held_v1'::text
    AS contract_version,
  materialized.owner_user_id,
  materialized.claim_id,
  claim.status::text AS claim_status,
  materialized.current_revision_number AS claim_revision_number,
  materialized.projection_request_id,
  materialized.plan_id,
  materialized.projection_ref,
  materialized.projection_review_id,
  materialized.projection_review_manifest_sha256,
  materialized.projection_apply_event_id,
  materialized.projection_apply_manifest_sha256,
  materialized.candidate_revision_id,
  materialized.candidate_revision_number,
  materialized.candidate_claim_state_sha256,
  materialized.observation_count,
  materialized.observation_authority_sha256,
  review_request.request_id AS assessment_review_request_id,
  review.review_id AS assessment_review_id,
  review.reviewer_type,
  review.reviewer_ref,
  review.action::text AS action,
  review.expected_from_status::text AS expected_from_status,
  review.target_status::text AS target_status,
  review.authorization_manifest_sha256
    AS assessment_review_manifest_sha256,
  assessment_apply.request_id AS assessment_apply_request_id,
  assessment_apply.event_id AS assessment_apply_event_id,
  assessment_apply.assessment_id,
  assessment_apply.apply_manifest_sha256
    AS assessment_apply_manifest_sha256,
  supported_revision.revision_id AS supported_revision_id,
  supported_revision.revision_number AS supported_revision_number,
  outbox.outbox_id,
  true AS projection_eligibility_held,
  false AS projection_worker_claimable
FROM memory.read_governed_claim_materialization_authority_v1 AS materialized
JOIN memory.claim AS claim
  ON claim.owner_user_id=materialized.owner_user_id
 AND claim.claim_id=materialized.claim_id
JOIN memory.claim_assessment_review_v5 AS review
  ON review.owner_user_id=materialized.owner_user_id
 AND review.claim_id=materialized.claim_id
JOIN memory.relational_operation_request AS review_request
  ON review_request.owner_user_id=review.owner_user_id
 AND review_request.operation='review_claim_assessment_v5'
 AND review_request.target_key=review.claim_id::text
 AND review_request.manifest_sha256=review.authorization_manifest_sha256
JOIN memory.claim_assessment_apply_v5 AS assessment_apply
  ON assessment_apply.owner_user_id=review.owner_user_id
 AND assessment_apply.claim_id=review.claim_id
 AND assessment_apply.review_id=review.review_id
JOIN memory.relational_operation_request AS apply_request
  ON apply_request.owner_user_id=assessment_apply.owner_user_id
 AND apply_request.request_id=assessment_apply.request_id
 AND apply_request.operation='apply_claim_assessment_v5'
 AND apply_request.target_key=assessment_apply.claim_id::text
 AND apply_request.manifest_sha256=assessment_apply.apply_manifest_sha256
JOIN memory.claim_assessment AS assessment
  ON assessment.owner_user_id=assessment_apply.owner_user_id
 AND assessment.assessment_id=assessment_apply.assessment_id
 AND assessment.claim_id=assessment_apply.claim_id
JOIN memory.claim_revision AS supported_revision
  ON supported_revision.owner_user_id=assessment_apply.owner_user_id
 AND supported_revision.claim_id=assessment_apply.claim_id
 AND supported_revision.revision_number=
       assessment_apply.resulting_revision_number
JOIN memory.projection_outbox AS outbox
  ON outbox.owner_user_id=claim.owner_user_id
 AND outbox.aggregate_type='claim'
 AND outbox.aggregate_id=claim.claim_id
 AND outbox.operation='upsert'
WHERE session_user='brains_app'
  AND materialized.owner_user_id=(SELECT memory.current_actor_user_id())
  AND materialized.claim_status='supported'
  AND materialized.current_revision_number=2
  AND materialized.candidate_revision_number=1
  AND review.action='promote_supported'
  AND review.expected_from_status='candidate'
  AND review.target_status='supported'
  AND review.expected_revision_number=1
  AND review.expected_claim_state_sha256=
      materialized.candidate_claim_state_sha256
  AND review.expected_evidence_manifest_sha256=
      materialized.observation_authority_sha256
  AND review.support_score>=0.500
  AND review.claim_confidence>=0.500
  AND review.opposition_score<=review.support_score
  AND review.reviewer_type='user'
  AND review.reviewer_ref=materialized.owner_user_id::text
  AND review.authorization_manifest_sha256=memory.v5_digest_text(
    pg_catalog.concat_ws('|',
      'memory_v1_claim_assessment_review_v5',
      materialized.owner_user_id::text,
      materialized.claim_id::text,
      review.action::text,
      review.expected_from_status::text,
      review.target_status::text,
      review.expected_revision_number::text,
      materialized.candidate_claim_state_sha256,
      materialized.observation_authority_sha256,
      materialized.projection_apply_event_id::text,
      materialized.projection_apply_manifest_sha256,
      review.support_score::text,
      review.opposition_score::text,
      review.claim_confidence::text,
      review.assessment_confidence::text,
      memory.v5_canonical_json_text(review.reason_codes),
      pg_catalog.btrim(review.rationale),
      review.reviewer_type,
      review.reviewer_ref
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM memory.claim_assessment_review_v5 AS later_review
    WHERE later_review.owner_user_id=review.owner_user_id
      AND later_review.claim_id=review.claim_id
      AND (
        later_review.created_at>review.created_at
        OR (later_review.created_at=review.created_at
            AND later_review.review_id>review.review_id)
      )
  )
  AND review_request.request_id<>materialized.projection_request_id
  AND review_request.outcome='applied'
  AND review_request.invoked_by_session='brains_app'
  AND review_request.result=pg_catalog.jsonb_build_object(
    'review_id',review.review_id,
    'claim_id',review.claim_id,
    'target_status',review.target_status
  )
  AND assessment_apply.request_id<>materialized.projection_request_id
  AND assessment_apply.request_id<>review_request.request_id
  AND assessment_apply.from_status='candidate'
  AND assessment_apply.to_status='supported'
  AND assessment_apply.prior_revision_number=1
  AND assessment_apply.resulting_revision_number=2
  AND assessment_apply.invoked_by_session='brains_app'
  AND assessment_apply.apply_manifest_sha256=memory.v5_digest_text(
    pg_catalog.concat_ws('|',
      'memory_v1_claim_assessment_apply_v5',
      materialized.owner_user_id::text,
      materialized.claim_id::text,
      review.review_id::text,
      review.authorization_manifest_sha256,
      review.expected_from_status::text,
      review.target_status::text,
      materialized.candidate_revision_number::text,
      materialized.candidate_claim_state_sha256,
      materialized.observation_authority_sha256
    )
  )
  AND apply_request.outcome='applied'
  AND apply_request.invoked_by_session='brains_app'
  AND apply_request.result=pg_catalog.jsonb_build_object(
    'event_id',assessment_apply.event_id,
    'assessment_id',assessment_apply.assessment_id,
    'claim_id',assessment_apply.claim_id,
    'from_status',assessment_apply.from_status,
    'target_status',assessment_apply.to_status,
    'resulting_revision_number',assessment_apply.resulting_revision_number
  )
  AND assessment.status='supported'
  AND assessment.support_score=review.support_score
  AND assessment.opposition_score=review.opposition_score
  AND assessment.confidence=review.assessment_confidence
  AND assessment.method='memory_v1_claim_assessment_v5'
  AND assessment.method_version='v5.1'
  AND assessment.rationale=review.rationale
  AND assessment.inputs=pg_catalog.jsonb_build_object(
    'review_id',review.review_id,
    'reason_codes',review.reason_codes,
    'claim_state_sha256',review.expected_claim_state_sha256,
    'evidence_manifest_sha256',review.expected_evidence_manifest_sha256
  )
  AND assessment.supersedes_assessment_id IS NULL
  AND supported_revision.reason='claim_assessment_v5:promote_supported'
  AND supported_revision.actor_type='user'
  AND supported_revision.actor_ref=materialized.owner_user_id::text
  AND supported_revision.snapshot=pg_catalog.to_jsonb(claim)
  AND claim.status='supported'
  AND claim.confidence=review.claim_confidence
  AND claim.last_confirmed_at IS NOT NULL
  AND outbox.payload=pg_catalog.jsonb_build_object(
    'claim_id',claim.claim_id,
    'revision_number',2
  )
  AND outbox.status='pending'
  AND outbox.attempts=0
  AND outbox.available_at='infinity'::timestamptz
  AND outbox.last_error IS NULL
  AND outbox.lease_token IS NULL
  AND outbox.lease_expires_at IS NULL
  AND outbox.worker_id IS NULL
  AND outbox.created_at=outbox.updated_at
  AND NOT (
    (outbox.status IN ('pending','error')
     AND outbox.available_at<=pg_catalog.clock_timestamp())
    OR
    (outbox.status='processing'
     AND outbox.lease_expires_at<=pg_catalog.clock_timestamp())
  )
  AND (SELECT pg_catalog.count(*)
       FROM memory.claim_revision AS revision_count
       WHERE revision_count.owner_user_id=claim.owner_user_id
         AND revision_count.claim_id=claim.claim_id)=2
  AND (SELECT pg_catalog.count(*)
       FROM memory.claim_assessment_review_v5 AS review_count
       WHERE review_count.owner_user_id=claim.owner_user_id
         AND review_count.claim_id=claim.claim_id)=1
  AND (SELECT pg_catalog.count(*)
       FROM memory.claim_assessment_apply_v5 AS apply_count
       WHERE apply_count.owner_user_id=claim.owner_user_id
         AND apply_count.claim_id=claim.claim_id)=1
  AND (SELECT pg_catalog.count(*)
       FROM memory.claim_assessment AS assessment_count
       WHERE assessment_count.owner_user_id=claim.owner_user_id
         AND assessment_count.claim_id=claim.claim_id)=1
  AND (SELECT pg_catalog.count(*)
       FROM memory.projection_outbox AS outbox_count
       WHERE outbox_count.owner_user_id=claim.owner_user_id
         AND outbox_count.aggregate_type='claim'
         AND outbox_count.aggregate_id=claim.claim_id
         AND outbox_count.operation='upsert')=1;

ALTER TABLE memory.read_governed_claim_supported_held_v1
  OWNER TO memory_v5_writer;
GRANT SELECT ON memory.read_governed_claim_supported_held_v1
  TO brains_app;
