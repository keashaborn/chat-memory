BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole(
       'memory_v5_2_reviewed_observation_stage_maintainer'
     ) IS NULL
     OR to_regclass(
       'memory.entity_resolution_reconciliation_v5_2'
     ) IS NULL
     OR to_regclass('memory.entity_role_resolution_v5_1') IS NULL
     OR to_regclass('memory.owner_packet_feedback_v1') IS NULL
     OR to_regprocedure(
       'memory.v5_2_reviewed_observation_stage_source_v1(uuid,uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION
      'reviewed-stage successor compatibility prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.owner_packet_stage_eligible_v1(p_packet_id uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'packet stage eligibility requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_packet_id IS NULL THEN
    RAISE EXCEPTION 'packet ID is required'
      USING ERRCODE='22004';
  END IF;

  RETURN NOT EXISTS (
    SELECT 1
    FROM memory.owner_packet_feedback_v1 AS feedback
    WHERE feedback.owner_user_id=actor
      AND feedback.packet_id=p_packet_id
      AND feedback.decision='not_correct'
      AND NOT EXISTS (
        SELECT 1
        FROM memory.owner_packet_feedback_v1 AS successor
        WHERE successor.owner_user_id=feedback.owner_user_id
          AND successor.supersedes_feedback_id=feedback.feedback_id
      )
  );
END
$function$;

ALTER FUNCTION memory.owner_packet_stage_eligible_v1(uuid)
  OWNER TO sage;
REVOKE ALL ON FUNCTION memory.owner_packet_stage_eligible_v1(uuid)
FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION memory.owner_packet_stage_eligible_v1(uuid)
TO memory_v5_2_reviewed_observation_stage_maintainer;

CREATE OR REPLACE FUNCTION
memory.v5_2_resolution_successor_source_v1(
  p_source_resolution_id uuid
)
RETURNS TABLE(
  source_kind text,
  successor_resolution_id uuid,
  target_entity_id uuid,
  successor_action memory.entity_resolution_action,
  reconciliation_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION
      'resolution-successor source requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_source_resolution_id IS NULL THEN
    RAISE EXCEPTION 'source resolution ID is required'
      USING ERRCODE='22004';
  END IF;

  RETURN QUERY
  SELECT
    'entity_resolution_reconciliation_v5_2'::text,
    value.successor_resolution_id,
    value.target_entity_id,
    'link_existing'::memory.entity_resolution_action,
    value.reconciliation_manifest_sha256
  FROM memory.entity_resolution_reconciliation_v5_2 AS value
  WHERE value.owner_user_id=actor
    AND value.source_resolution_id=p_source_resolution_id
  UNION ALL
  SELECT
    'entity_role_resolution_v5_1'::text,
    value.successor_resolution_id,
    value.target_entity_id,
    value.successor_action,
    value.reconciliation_manifest_sha256
  FROM memory.entity_role_resolution_v5_1 AS value
  WHERE value.owner_user_id=actor
    AND value.source_resolution_id=p_source_resolution_id;
END
$function$;

ALTER FUNCTION memory.v5_2_resolution_successor_source_v1(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION
  memory.v5_2_resolution_successor_source_v1(uuid)
FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION
  memory.v5_2_resolution_successor_source_v1(uuid)
TO memory_v5_2_reviewed_observation_stage_maintainer;

CREATE OR REPLACE FUNCTION
memory.v5_2_reviewed_observation_stage_source_v1(
  p_route_event_id uuid,
  p_batch_id uuid
)
RETURNS TABLE(
  source_kind text,
  route_event_id uuid,
  atom_apply_id uuid,
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  batch_id uuid,
  stage_request_id uuid,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  packet_storage_sha256 text,
  repository_commit text,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  observation_state_sha256 text,
  entity_mention_count integer,
  observation_count integer,
  resolution_count integer,
  approved_review_count integer,
  binding_count integer,
  source_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation source requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;

  RETURN QUERY
  WITH base AS (
    SELECT
      CASE
        WHEN atom.apply_id IS NOT NULL
          AND atom.stage_projection_sha256=batch.extraction_packet_sha256
          AND batch.extractor IN (
            'memory_v1_v5_2_atom_stage_projection',
            'memory_v1_v5_2_atom_stage_projection_v2'
          )
          THEN 'atom_apply'::text
        WHEN atom.apply_id IS NULL
          AND route.blocking_code_count=0
          AND route.request_id=request.request_id
          AND route.validator_packet_sha256=batch.extraction_packet_sha256
          AND batch.extractor='memory_v1_v5_2_local_packet_review'
          THEN 'reviewed_route'::text
        ELSE NULL::text
      END AS source_kind,
      route.route_event_id,atom.apply_id AS atom_apply_id,
      route.packet_id,route.job_id,route.evidence_id,batch.batch_id,
      request.request_id AS stage_request_id,
      route.review_report_sha256,route.stage_bundle_sha256,
      route.packet_storage_sha256,route.repository_commit,
      batch.stage_manifest_sha256,batch.mention_count,
      batch.observation_count,batch.resolution_count,batch.result,
      greatest(route.created_at,batch.created_at) AS source_created_at
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=route.owner_user_id
     AND packet.packet_id=route.packet_id
     AND packet.job_id=route.job_id
     AND packet.evidence_id=route.evidence_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=route.owner_user_id
     AND job.job_id=route.job_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=route.owner_user_id
     AND evidence.evidence_id=route.evidence_id
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=route.owner_user_id
     AND batch.batch_id=p_batch_id
     AND batch.evidence_id=route.evidence_id
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=batch.owner_user_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
     AND request.result->>'batch_id'=batch.batch_id::text
     AND request.manifest_sha256=batch.stage_manifest_sha256
    LEFT JOIN memory.v5_2_atom_admission_apply AS atom
      ON atom.owner_user_id=route.owner_user_id
     AND atom.packet_id=route.packet_id
    LEFT JOIN memory.v5_2_atom_admission_proposal AS proposal
      ON proposal.owner_user_id=atom.owner_user_id
     AND proposal.proposal_id=atom.proposal_id
     AND proposal.packet_id=route.packet_id
     AND proposal.evidence_id=route.evidence_id
     AND proposal.stage_projection_sha256=atom.stage_projection_sha256
    LEFT JOIN memory.v5_2_atom_admission_review AS atom_review
      ON atom_review.owner_user_id=atom.owner_user_id
     AND atom_review.review_id=atom.review_id
     AND atom_review.proposal_id=atom.proposal_id
     AND atom_review.decision='authorized'
    WHERE route.owner_user_id=actor
      AND route.route_event_id=p_route_event_id
      AND route.route='manual_review_artifact_ready'
      AND route.reason_code='reviewable_relational_packet_v5_2'
      AND route.review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND route.bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND packet.packet_storage_sha256=route.packet_storage_sha256
      AND packet.evidence_content_sha256=evidence.content_sha256
      AND packet.external_model_calls=0
      AND memory.owner_packet_stage_eligible_v1(route.packet_id)
      AND job.status='review_required'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status='active'
      AND evidence.content IS NOT NULL
      AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
      AND jsonb_typeof(batch.result->'resolution_ids')='object'
      AND jsonb_typeof(batch.result->'observation_ids')='object'
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'resolution_ids'))
          =batch.resolution_count
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'observation_ids'))
          =batch.observation_count
      AND (
        atom.apply_id IS NULL
        OR (
          proposal.proposal_id IS NOT NULL
          AND atom_review.review_id IS NOT NULL
          AND atom.proposal_sha256=proposal.proposal_sha256
          AND atom.review_authorization_manifest_sha256=
            atom_review.authorization_manifest_sha256
        )
      )
  ), resolution_rows AS (
    SELECT
      base.route_event_id,
      source_plan.resolution_id AS source_resolution_id,
      mapping.mapping_count,
      mapping.source_kind AS reconciliation_source_kind,
      mapping.successor_resolution_id,
      mapping.target_entity_id AS reconciliation_target_entity_id,
      mapping.successor_action AS reconciliation_successor_action,
      mapping.reconciliation_manifest_sha256,
      coalesce(
        successor_plan.resolution_id,source_plan.resolution_id
      ) AS effective_resolution_id,
      coalesce(successor_plan.action,source_plan.action) AS effective_action,
      coalesce(
        successor_plan.decision_state,source_plan.decision_state
      ) AS effective_decision_state,
      effective_apply.applied_entity_id,
      effective_apply.apply_manifest_sha256,
      effective_review.review_id,
      effective_review.decision AS review_decision,
      effective_review.authorization_manifest_sha256,
      CASE
        WHEN mapping.mapping_count>1 THEN true
        WHEN mapping.mapping_count=0 THEN false
        WHEN mapping.successor_resolution_id IS NULL
          OR successor_plan.resolution_id IS NULL
          OR source_apply.resolution_id IS NOT NULL
          OR successor_plan.evidence_id<>source_plan.evidence_id
          OR successor_plan.mention_id<>source_plan.mention_id
          OR successor_plan.action<>mapping.successor_action
          OR (
            mapping.successor_action='link_existing'
            AND (
              mapping.target_entity_id IS NULL
              OR successor_plan.selected_entity_id
                   IS DISTINCT FROM mapping.target_entity_id
            )
          )
          OR (
            mapping.successor_action='create_new'
            AND (
              mapping.target_entity_id IS NOT NULL
              OR successor_plan.selected_entity_id IS NOT NULL
              OR successor_plan.proposed_entity IS NULL
            )
          ) THEN true
        ELSE false
      END AS invalid_reconciliation
    FROM base
    JOIN LATERAL jsonb_each_text(
      base.result->'resolution_ids'
    ) AS ids ON true
    JOIN memory.entity_resolution_plan AS source_plan
      ON source_plan.owner_user_id=actor
     AND source_plan.resolution_id=ids.value::uuid
     AND source_plan.evidence_id=base.evidence_id
    LEFT JOIN LATERAL (
      SELECT
        count(*)::integer AS mapping_count,
        (array_agg(value.source_kind
          ORDER BY value.source_kind))[1] AS source_kind,
        (array_agg(value.successor_resolution_id
          ORDER BY value.source_kind))[1] AS successor_resolution_id,
        (array_agg(value.target_entity_id
          ORDER BY value.source_kind))[1] AS target_entity_id,
        (array_agg(value.successor_action
          ORDER BY value.source_kind))[1] AS successor_action,
        (array_agg(value.reconciliation_manifest_sha256
          ORDER BY value.source_kind))[1] AS reconciliation_manifest_sha256
      FROM memory.v5_2_resolution_successor_source_v1(
        source_plan.resolution_id
      ) AS value
    ) AS mapping ON true
    LEFT JOIN memory.entity_resolution_plan AS successor_plan
      ON successor_plan.owner_user_id=source_plan.owner_user_id
     AND successor_plan.resolution_id=mapping.successor_resolution_id
    LEFT JOIN memory.entity_resolution_apply AS source_apply
      ON source_apply.owner_user_id=source_plan.owner_user_id
     AND source_apply.resolution_id=source_plan.resolution_id
    LEFT JOIN memory.entity_resolution_apply AS effective_apply
      ON effective_apply.owner_user_id=source_plan.owner_user_id
     AND effective_apply.resolution_id=coalesce(
       successor_plan.resolution_id,source_plan.resolution_id
     )
    LEFT JOIN memory.entity_resolution_review AS effective_review
      ON effective_review.owner_user_id=effective_apply.owner_user_id
     AND effective_review.resolution_id=effective_apply.resolution_id
     AND effective_review.review_id=effective_apply.review_id
  ), resolution_state AS (
    SELECT
      row_value.route_event_id,
      count(*)::integer AS exact_resolution_count,
      count(row_value.applied_entity_id)::integer AS applied_count,
      count(*) FILTER (
        WHERE row_value.effective_decision_state='manual_review_required'
      )::integer AS manual_count,
      count(*) FILTER (
        WHERE row_value.effective_decision_state='manual_review_required'
          AND row_value.review_id IS NOT NULL
          AND row_value.review_decision='approved'
      )::integer AS approved_review_count,
      count(*) FILTER (
        WHERE row_value.invalid_reconciliation
           OR row_value.effective_action NOT IN (
             'link_existing','create_new'
           )
           OR row_value.effective_decision_state NOT IN (
             'auto_link_eligible','manual_review_required'
           )
      )::integer AS invalid_resolution_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        row_value.source_resolution_id::text,
        coalesce(row_value.reconciliation_source_kind,''),
        coalesce(row_value.successor_resolution_id::text,''),
        coalesce(row_value.reconciliation_manifest_sha256,''),
        row_value.effective_resolution_id::text,
        row_value.effective_action::text,
        row_value.effective_decision_state::text,
        coalesce(row_value.applied_entity_id::text,''),
        coalesce(row_value.apply_manifest_sha256,''),
        coalesce(row_value.review_id::text,''),
        coalesce(row_value.authorization_manifest_sha256,'')
      ),E'\n' ORDER BY row_value.source_resolution_id),''))
        AS state_sha256
    FROM resolution_rows AS row_value
    GROUP BY row_value.route_event_id
  ), observation_state AS (
    SELECT
      base.route_event_id,
      count(*)::integer AS exact_observation_count,
      count(binding.observation_id)::integer AS binding_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        observation.observation_id::text,
        observation.observation_sha256,
        coalesce(binding.binding_manifest_sha256,'')
      ),E'\n' ORDER BY observation.observation_id),'')) AS state_sha256
    FROM base
    JOIN LATERAL jsonb_each_text(
      base.result->'observation_ids'
    ) AS ids ON true
    JOIN memory.observation AS observation
      ON observation.owner_user_id=actor
     AND observation.observation_id=ids.value::uuid
     AND observation.evidence_id=base.evidence_id
     AND observation.observation_ref=ids.key
     AND observation.packet_sha256=(
       SELECT exact_batch.extraction_packet_sha256
       FROM memory.relational_stage_batch AS exact_batch
       WHERE exact_batch.owner_user_id=actor
         AND exact_batch.batch_id=base.batch_id
     )
     AND observation.projection_class<>'never_surface'
    LEFT JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=observation.owner_user_id
     AND binding.observation_id=observation.observation_id
    GROUP BY base.route_event_id
  )
  SELECT base.source_kind,base.route_event_id,base.atom_apply_id,
    base.packet_id,base.job_id,base.evidence_id,base.batch_id,
    base.stage_request_id,base.review_report_sha256,
    base.stage_bundle_sha256,base.packet_storage_sha256,
    base.repository_commit,base.stage_manifest_sha256,
    resolution_state.state_sha256,observation_state.state_sha256,
    base.mention_count::integer,base.observation_count::integer,
    base.resolution_count::integer,
    resolution_state.approved_review_count,
    observation_state.binding_count,base.source_created_at
  FROM base
  JOIN resolution_state USING(route_event_id)
  JOIN observation_state USING(route_event_id)
  WHERE base.source_kind IS NOT NULL
    AND resolution_state.exact_resolution_count=base.resolution_count
    AND resolution_state.applied_count=base.resolution_count
    AND resolution_state.manual_count=
      resolution_state.approved_review_count
    AND resolution_state.invalid_resolution_count=0
    AND observation_state.exact_observation_count=base.observation_count
    AND observation_state.binding_count=base.observation_count;
END
$function$;

ALTER FUNCTION
  memory.v5_2_reviewed_observation_stage_source_v1(uuid,uuid)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;

DO $postflight$
BEGIN
  IF has_function_privilege(
       'brains_app',
       'memory.owner_packet_stage_eligible_v1(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_v5_2_reviewed_observation_stage_maintainer',
       'memory.owner_packet_stage_eligible_v1(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.v5_2_resolution_successor_source_v1(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'memory_v5_2_reviewed_observation_stage_maintainer',
       'memory.v5_2_resolution_successor_source_v1(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'resolution-successor helper ACL is unsafe';
  END IF;
END
$postflight$;

COMMIT;
