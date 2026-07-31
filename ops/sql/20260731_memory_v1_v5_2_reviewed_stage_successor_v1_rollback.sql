BEGIN;

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
  ), resolution_state AS (
    SELECT
      base.route_event_id,
      count(*)::integer AS exact_resolution_count,
      count(applied.resolution_id)::integer AS applied_count,
      count(*) FILTER (
        WHERE plan.decision_state='manual_review_required'
      )::integer AS manual_count,
      count(*) FILTER (
        WHERE plan.decision_state='manual_review_required'
          AND review.review_id IS NOT NULL
          AND review.decision='approved'
      )::integer AS approved_review_count,
      count(*) FILTER (
        WHERE plan.action NOT IN ('link_existing','create_new')
           OR plan.decision_state NOT IN (
             'auto_link_eligible','manual_review_required'
           )
      )::integer AS invalid_resolution_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        plan.resolution_id::text,plan.action::text,
        plan.decision_state::text,
        coalesce(applied.applied_entity_id::text,''),
        coalesce(applied.apply_manifest_sha256,''),
        coalesce(review.review_id::text,''),
        coalesce(review.authorization_manifest_sha256,'')
      ),E'\n' ORDER BY plan.resolution_id),'')) AS state_sha256
    FROM base
    JOIN LATERAL jsonb_each_text(
      base.result->'resolution_ids'
    ) AS ids ON true
    JOIN memory.entity_resolution_plan AS plan
      ON plan.owner_user_id=actor
     AND plan.resolution_id=ids.value::uuid
     AND plan.evidence_id=base.evidence_id
    LEFT JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id=plan.owner_user_id
     AND applied.resolution_id=plan.resolution_id
    LEFT JOIN memory.entity_resolution_review AS review
      ON review.owner_user_id=applied.owner_user_id
     AND review.resolution_id=applied.resolution_id
     AND review.review_id=applied.review_id
    GROUP BY base.route_event_id
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

DROP FUNCTION memory.v5_2_resolution_successor_source_v1(uuid);
DROP FUNCTION memory.owner_packet_stage_eligible_v1(uuid);

COMMIT;
