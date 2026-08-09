CREATE FUNCTION memory.retract_owner_governed_claim_v1(p_review_request_id uuid, p_apply_request_id uuid, p_claim_id uuid, p_expected_revision_number integer, p_expected_claim_state_sha256 text, p_reason text)
RETURNS TABLE(
  outcome text,
  event_id uuid,
  resulting_revision_number integer,
  outbox_id uuid,
  outbox_dispatch_held boolean
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  state jsonb;
  review_preflight record;
  reviewed record;
  apply_preflight record;
  applied record;
  prior_review_request memory.relational_operation_request%ROWTYPE;
  prior_apply_request memory.relational_operation_request%ROWTYPE;
  outbox memory.projection_outbox%ROWTYPE;
  reason_codes constant jsonb := '["owner_requested_retraction"]'::jsonb;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_review_request_id IS NULL
     OR p_apply_request_id IS NULL
     OR p_review_request_id=p_apply_request_id
     OR p_claim_id IS NULL
     OR p_expected_revision_number IS NULL
     OR p_expected_revision_number<1
     OR NOT memory.v5_sha256_valid(p_expected_claim_state_sha256)
     OR btrim(COALESCE(p_reason,''))=''
     OR length(p_reason)>500 THEN
    RAISE EXCEPTION 'governed claim retraction inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|governed_claim_retraction|'||p_claim_id::text,0
  ));

  SELECT request.* INTO prior_review_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_review_request_id;
  SELECT request.* INTO prior_apply_request
  FROM memory.relational_operation_request AS request
  WHERE request.owner_user_id=actor
    AND request.request_id=p_apply_request_id;

  IF prior_review_request.request_id IS NOT NULL
     OR prior_apply_request.request_id IS NOT NULL THEN
    IF prior_review_request.request_id IS NULL
       OR prior_apply_request.request_id IS NULL
       OR prior_review_request.operation<>'review_claim_assessment_v5'
       OR prior_apply_request.operation<>'apply_claim_assessment_v5'
       OR prior_review_request.target_key<>p_claim_id::text
       OR prior_apply_request.target_key<>p_claim_id::text THEN
      RAISE EXCEPTION 'governed claim retraction replay is incomplete or mismatched'
        USING ERRCODE='23514';
    END IF;
    SELECT value.* INTO outbox
    FROM memory.projection_outbox AS value
    WHERE value.owner_user_id=actor
      AND value.aggregate_type='claim'
      AND value.aggregate_id=p_claim_id
      AND value.operation='delete';
    IF outbox.outbox_id IS NULL THEN
      RAISE EXCEPTION 'governed claim retraction replay lacks derived delete event'
        USING ERRCODE='23514';
    END IF;
    IF outbox.payload->>'contract_version'
         <>'memory_v1_governed_claim_retraction_delete_v1'
       OR (outbox.payload->>'resulting_revision_number')::integer
         <>(prior_apply_request.result->>'resulting_revision_number')::integer
       OR outbox.available_at<>'infinity'::timestamptz THEN
      RAISE EXCEPTION 'governed claim retraction replay payload drifted'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      'replayed'::text,
      (prior_apply_request.result->>'event_id')::uuid,
      (prior_apply_request.result->>'resulting_revision_number')::integer,
      outbox.outbox_id,
      outbox.available_at='infinity'::timestamptz;
    RETURN;
  END IF;

  state := memory.claim_assessment_state_v5(p_claim_id);
  IF (state->>'current_revision_number')::integer
       <>p_expected_revision_number
     OR state->>'claim_state_sha256'
       <>p_expected_claim_state_sha256 THEN
    RAISE EXCEPTION 'governed claim retraction expected state is stale'
      USING ERRCODE='40001';
  END IF;

  SELECT * INTO review_preflight
  FROM memory.preflight_claim_assessment_review_v5(
    p_claim_id,
    'retract'::memory.claim_assessment_action_v5,
    0.000,1.000,0.000,1.000,
    reason_codes,p_reason,'user',actor::text
  );
  SELECT * INTO reviewed
  FROM memory.review_claim_assessment_v5(
    p_review_request_id,p_claim_id,
    'retract'::memory.claim_assessment_action_v5,
    0.000,1.000,0.000,1.000,
    reason_codes,p_reason,'user',actor::text,
    review_preflight.authorization_manifest_sha256
  );
  SELECT * INTO apply_preflight
  FROM memory.preflight_claim_assessment_apply_v5(
    p_claim_id,reviewed.review_id
  );
  SELECT * INTO applied
  FROM memory.apply_claim_assessment_v5(
    p_apply_request_id,p_claim_id,reviewed.review_id,
    apply_preflight.apply_manifest_sha256
  );

  INSERT INTO memory.projection_outbox(
    owner_user_id,aggregate_type,aggregate_id,operation,payload,
    status,attempts,available_at
  ) VALUES (
    actor,'claim',p_claim_id,'delete',
    jsonb_build_object(
      'contract_version','memory_v1_governed_claim_retraction_delete_v1',
      'claim_id',p_claim_id,
      'resulting_revision_number',applied.resulting_revision_number,
      'reason_code','owner_requested_retraction'
    ),
    'pending'::memory.outbox_status,0,'infinity'::timestamptz
  )
  ON CONFLICT (owner_user_id,aggregate_type,aggregate_id,operation)
  DO NOTHING;

  SELECT value.* INTO STRICT outbox
  FROM memory.projection_outbox AS value
  WHERE value.owner_user_id=actor
    AND value.aggregate_type='claim'
    AND value.aggregate_id=p_claim_id
    AND value.operation='delete';
  IF outbox.payload->>'contract_version'
       <>'memory_v1_governed_claim_retraction_delete_v1'
     OR (outbox.payload->>'resulting_revision_number')::integer
       <>applied.resulting_revision_number THEN
    RAISE EXCEPTION 'governed claim retraction derived delete event drifted'
      USING ERRCODE='23514';
  END IF;

  RETURN QUERY SELECT
    applied.outcome::text,
    applied.event_id,
    applied.resulting_revision_number,
    outbox.outbox_id,
    outbox.available_at='infinity'::timestamptz;
END
$function$;

ALTER FUNCTION memory.retract_owner_governed_claim_v1(uuid,uuid,uuid,integer,text,text) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.retract_owner_governed_claim_v1(uuid,uuid,uuid,integer,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.retract_owner_governed_claim_v1(uuid,uuid,uuid,integer,text,text) TO brains_app;
