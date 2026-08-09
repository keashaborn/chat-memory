CREATE FUNCTION memory.release_owner_projection_outbox_write_v1(p_actor uuid, p_outbox_id uuid, p_claim_id uuid, p_expected_payload_sha256 text)
RETURNS TABLE(outbox_id uuid, claim_id uuid, payload_sha256 text, rows_written integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  target memory.projection_outbox%ROWTYPE;
  current_revision integer;
  observed_payload_sha256 text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'projection outbox release write requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL OR p_actor IS NULL OR actor<>p_actor
     OR p_outbox_id IS NULL OR p_claim_id IS NULL
     OR p_expected_payload_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'projection outbox release write inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  SELECT value.* INTO target
  FROM memory.projection_outbox AS value
  WHERE value.owner_user_id=actor AND value.outbox_id=p_outbox_id
  FOR UPDATE;
  IF target.outbox_id IS NULL OR target.aggregate_type<>'claim'
     OR target.aggregate_id<>p_claim_id OR target.operation<>'upsert'
     OR target.status<>'pending'::memory.outbox_status OR target.attempts<>0
     OR target.available_at<>'infinity'::timestamptz
     OR target.last_error IS NOT NULL OR target.lease_token IS NOT NULL
     OR target.lease_expires_at IS NOT NULL OR target.worker_id IS NOT NULL THEN
    RAISE EXCEPTION 'projection outbox item is not an exact pristine held upsert'
      USING ERRCODE='23514';
  END IF;
  SELECT pg_catalog.max(revision.revision_number)
  INTO current_revision
  FROM memory.claim_revision AS revision
  JOIN memory.claim AS claim ON claim.owner_user_id=revision.owner_user_id
    AND claim.claim_id=revision.claim_id
  WHERE revision.owner_user_id=actor AND revision.claim_id=p_claim_id
    AND claim.status='supported'::memory.claim_status;
  IF current_revision IS NULL
     OR target.payload<>pg_catalog.jsonb_build_object(
          'claim_id',p_claim_id,'revision_number',current_revision
        ) THEN
    RAISE EXCEPTION 'projection outbox item does not match the current supported claim'
      USING ERRCODE='23514';
  END IF;
  observed_payload_sha256:=pg_catalog.encode(public.digest(pg_catalog.convert_to(
    '{"claim_id":'||pg_catalog.to_jsonb(p_claim_id::text)::text||
    ',"revision_number":'||current_revision::text||'}','UTF8'
  ),'sha256'),'hex');
  IF observed_payload_sha256<>p_expected_payload_sha256 THEN
    RAISE EXCEPTION 'projection outbox payload hash mismatch'
      USING ERRCODE='23514';
  END IF;
  UPDATE memory.projection_outbox AS value
  SET available_at=pg_catalog.clock_timestamp(),updated_at=pg_catalog.clock_timestamp()
  WHERE value.owner_user_id=actor AND value.outbox_id=p_outbox_id
    AND value.available_at='infinity'::timestamptz;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'projection outbox release lost its exact target'
      USING ERRCODE='40001';
  END IF;
  RETURN QUERY SELECT p_outbox_id,p_claim_id,p_expected_payload_sha256,1;
END
$function$;

ALTER FUNCTION memory.release_owner_projection_outbox_write_v1(uuid,uuid,uuid,text) OWNER TO sage;
REVOKE ALL ON FUNCTION memory.release_owner_projection_outbox_write_v1(uuid,uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.release_owner_projection_outbox_write_v1(uuid,uuid,uuid,text) TO memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.release_owner_projection_outbox_write_v1(uuid,uuid,uuid,text) TO sage;
