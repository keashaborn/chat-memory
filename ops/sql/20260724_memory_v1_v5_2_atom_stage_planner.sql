BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner install requires sage';
  END IF;
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_2_atom_proposal_sha256_v1(jsonb)') IS NULL
     OR to_regprocedure(
       'memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)'
     ) IS NULL
     OR to_regclass('memory.v5_2_atom_admission_apply') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_review') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom stage planner prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_atom_stage_v1(
  p_apply_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  applied memory.v5_2_atom_admission_apply%ROWTYPE;
  proposal memory.v5_2_atom_admission_proposal%ROWTYPE;
  review memory.v5_2_atom_admission_review%ROWTYPE;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  projection jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_apply_id IS NULL THEN
    RAISE EXCEPTION 'apply_id is required' USING ERRCODE='22004';
  END IF;

  SELECT source.* INTO applied
  FROM memory.v5_2_atom_admission_apply AS source
  WHERE source.owner_user_id=actor
    AND source.apply_id=p_apply_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped authorized atom stage plan is unavailable'
      USING ERRCODE='P0002';
  END IF;

  SELECT source.* INTO STRICT proposal
  FROM memory.v5_2_atom_admission_proposal AS source
  WHERE source.owner_user_id=actor
    AND source.proposal_id=applied.proposal_id;
  SELECT source.* INTO STRICT review
  FROM memory.v5_2_atom_admission_review AS source
  WHERE source.owner_user_id=actor
    AND source.review_id=applied.review_id
    AND source.proposal_id=applied.proposal_id;
  SELECT source.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=actor
    AND source.packet_id=applied.packet_id
    AND source.evidence_id=applied.evidence_id;

  projection:=proposal.proposal->'stage_projection';
  IF review.decision<>'authorized'
     OR review.review_id IS DISTINCT FROM (
       SELECT latest.review_id
       FROM memory.v5_2_atom_admission_review AS latest
       WHERE latest.owner_user_id=actor
         AND latest.proposal_id=proposal.proposal_id
       ORDER BY latest.review_number DESC
       LIMIT 1
     )
     OR applied.packet_id IS DISTINCT FROM proposal.packet_id
     OR applied.evidence_id IS DISTINCT FROM proposal.evidence_id
     OR applied.proposal_sha256 IS DISTINCT FROM proposal.proposal_sha256
     OR applied.stage_projection_sha256
          IS DISTINCT FROM proposal.stage_projection_sha256
     OR applied.review_authorization_manifest_sha256
          IS DISTINCT FROM review.authorization_manifest_sha256
     OR review.proposal_sha256 IS DISTINCT FROM proposal.proposal_sha256
     OR proposal.packet_id IS DISTINCT FROM
       memory.authoritative_owner_v5_2_packet_id_v1(proposal.evidence_id) THEN
    RAISE EXCEPTION 'atom stage authorization chain is no longer current'
      USING ERRCODE='23514';
  END IF;

  IF NOT memory.v5_jsonb_exact_keys(proposal.proposal,ARRAY[
       'contract_version','policy_version','source_packet_id','source_job_id',
       'source_evidence_id','source_validator_packet_sha256',
       'source_packet_storage_sha256','source_atom_manifest_sha256',
       'atom_decisions','stage_projection','stage_projection_sha256',
       'counts','proposal_sha256'
     ])
     OR proposal.proposal->>'contract_version'
          <>'memory_v1_v5_2_atom_admission_proposal_v1'
     OR proposal.proposal->>'policy_version'<>proposal.policy_version
     OR proposal.proposal->>'source_packet_id'<>proposal.packet_id::text
     OR proposal.proposal->>'source_job_id'<>proposal.job_id::text
     OR proposal.proposal->>'source_evidence_id'<>proposal.evidence_id::text
     OR proposal.proposal->>'source_validator_packet_sha256'
          <>proposal.validator_packet_sha256
     OR proposal.proposal->>'source_packet_storage_sha256'
          <>proposal.packet_storage_sha256
     OR proposal.proposal->>'stage_projection_sha256'
          <>proposal.stage_projection_sha256
     OR proposal.proposal->>'proposal_sha256'<>proposal.proposal_sha256
     OR memory.v5_2_atom_proposal_sha256_v1(proposal.proposal)
          <>proposal.proposal_sha256 THEN
    RAISE EXCEPTION 'atom proposal payload or provenance is invalid'
      USING ERRCODE='23514';
  END IF;

  IF projection IS NULL
     OR NOT memory.v5_jsonb_exact_keys(projection,ARRAY[
       'contract_version','source_envelope','predicate_registry_version',
       'entity_mentions','observations','comparison_hints','deferrals',
       'packet_findings'
     ])
     OR projection->>'contract_version'
          <>'memory_v1_relational_extraction_v5_2'
     OR projection->>'predicate_registry_version'
          <>'memory_predicate_registry_v5_2'
     OR projection->'source_envelope'
          IS DISTINCT FROM packet.normalized_packet->'source_envelope'
     OR jsonb_typeof(projection->'entity_mentions')<>'array'
     OR jsonb_typeof(projection->'observations')<>'array'
     OR jsonb_typeof(projection->'comparison_hints')<>'array'
     OR jsonb_typeof(projection->'deferrals')<>'array'
     OR jsonb_typeof(projection->'packet_findings')<>'array'
     OR jsonb_array_length(projection->'entity_mentions')
          <>applied.admitted_entity_mention_count
     OR jsonb_array_length(projection->'observations')
          <>applied.admitted_observation_count
     OR jsonb_array_length(projection->'comparison_hints')
          <>applied.admitted_comparison_hint_count
     OR jsonb_array_length(projection->'deferrals')<>0
     OR jsonb_array_length(projection->'packet_findings')<>0
     OR memory.v5_digest_text(memory.v5_canonical_json_text(projection))
          <>applied.stage_projection_sha256 THEN
    RAISE EXCEPTION 'authorized atom stage projection is invalid'
      USING ERRCODE='23514';
  END IF;

  IF packet.packet_storage_sha256<>proposal.packet_storage_sha256
     OR packet.validator_packet_sha256<>proposal.validator_packet_sha256
     OR NOT EXISTS (
       SELECT 1
       FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=proposal.packet_id
         AND route.evidence_id=proposal.evidence_id
         AND route.route='manual_review_artifact_ready'
         AND route.reason_code='reviewable_relational_packet_v5_2'
     )
     OR EXISTS (
       SELECT 1
       FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=proposal.packet_id
         AND route.evidence_id=proposal.evidence_id
         AND route.route='terminal_no_stage'
     )
     OR NOT memory.v5_2_atom_stage_projection_authorized_v1(
       actor,proposal.evidence_id,projection,proposal.stage_projection_sha256
     ) THEN
    RAISE EXCEPTION 'atom stage projection is not currently stage-authorized'
      USING ERRCODE='23514';
  END IF;

  RETURN jsonb_build_object(
    'contract_version','memory_v1_v5_2_atom_stage_plan_v1',
    'policy_version','memory_v1_v5_2_atom_stage_policy_v1',
    'owner_user_id',actor::text,
    'apply_id',applied.apply_id::text,
    'proposal_id',proposal.proposal_id::text,
    'review_id',review.review_id::text,
    'packet_id',proposal.packet_id::text,
    'evidence_id',proposal.evidence_id::text,
    'apply_manifest_sha256',applied.apply_manifest_sha256,
    'review_authorization_manifest_sha256',
      review.authorization_manifest_sha256,
    'proposal_sha256',proposal.proposal_sha256,
    'source_packet_storage_sha256',proposal.packet_storage_sha256,
    'source_validator_packet_sha256',proposal.validator_packet_sha256,
    'stage_projection_sha256',proposal.stage_projection_sha256,
    'stage_projection_text',memory.v5_canonical_json_text(projection),
    'stage_projection',projection,
    'mention_sha256_by_ref',COALESCE((
      SELECT jsonb_object_agg(
        mention.value->>'entity_ref',
        memory.v5_digest_text(memory.v5_canonical_json_text(mention.value))
        ORDER BY mention.ordinality
      )
      FROM jsonb_array_elements(projection->'entity_mentions')
        WITH ORDINALITY AS mention(value,ordinality)
    ),'{}'::jsonb),
    'counts',jsonb_build_object(
      'entity_mentions',applied.admitted_entity_mention_count,
      'observations',applied.admitted_observation_count,
      'comparison_hints',applied.admitted_comparison_hint_count,
      'deferrals',0
    )
  );
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_atom_stage_v1(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;

REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_atom_stage_v1(uuid)
  FROM PUBLIC,memory_v5_writer,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_atom_stage_v1(uuid)
  TO brains_app;

COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_stage_v1(uuid) IS
  'Returns one exact, current, owner-scoped V5.2 stage projection through a typed read-only boundary. It does not stage rows or authorize any different projection.';

COMMIT;
