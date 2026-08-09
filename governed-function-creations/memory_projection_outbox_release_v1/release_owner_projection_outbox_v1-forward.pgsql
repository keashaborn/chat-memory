CREATE FUNCTION memory.release_owner_projection_outbox_v1(p_release_request_id uuid, p_outbox_id uuid, p_claim_id uuid, p_expected_payload_sha256 text, p_reason text, p_release_manifest_sha256 text)
RETURNS TABLE(outcome text, outbox_id uuid, claim_id uuid, payload_sha256 text, release_manifest_sha256 text, rows_written integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  prior memory.relational_operation_request%ROWTYPE;
  released record;
  expected_manifest text;
  expected_result jsonb;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'projection outbox release requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required' USING ERRCODE='42501';
  END IF;
  IF p_release_request_id IS NULL OR p_outbox_id IS NULL OR p_claim_id IS NULL
     OR p_expected_payload_sha256 !~ '^[0-9a-f]{64}$'
     OR btrim(COALESCE(p_reason,''))='' OR length(p_reason)>500
     OR p_release_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'projection outbox release inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  expected_manifest:=memory.v5_digest_text(pg_catalog.concat_ws('|',
    'memory_v1_projection_outbox_release_v1',actor::text,
    p_release_request_id::text,p_outbox_id::text,p_claim_id::text,
    p_expected_payload_sha256,p_reason
  ));
  IF expected_manifest<>p_release_manifest_sha256 THEN
    RAISE EXCEPTION 'projection outbox release manifest mismatch'
      USING ERRCODE='23514';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    actor::text||'|projection_outbox_release|'||p_outbox_id::text,0
  ));
  expected_result:=pg_catalog.jsonb_build_object(
    'contract_version','memory_v1_projection_outbox_release_receipt_v1',
    'release_request_id',p_release_request_id,'outbox_id',p_outbox_id,
    'claim_id',p_claim_id,'expected_payload_sha256',p_expected_payload_sha256,
    'dispatch_scope','exact_item_only'
  );
  SELECT value.* INTO prior
  FROM memory.relational_operation_request AS value
  WHERE value.owner_user_id=actor AND value.request_id=p_release_request_id;
  IF prior.request_id IS NOT NULL THEN
    IF prior.operation<>'release_projection_outbox_v1'
       OR prior.target_key<>p_outbox_id::text
       OR prior.manifest_sha256<>p_release_manifest_sha256
       OR prior.outcome<>'applied' OR prior.result<>expected_result
       OR prior.invoked_by_session<>'brains_app' THEN
      RAISE EXCEPTION 'projection outbox release replay drifted'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text,p_outbox_id,p_claim_id,
      p_expected_payload_sha256,p_release_manifest_sha256,0;
    RETURN;
  END IF;
  SELECT value.* INTO released
  FROM memory.release_owner_projection_outbox_write_v1(
    actor,p_outbox_id,p_claim_id,p_expected_payload_sha256
  ) AS value;
  IF released.outbox_id IS NULL OR released.rows_written<>1 THEN
    RAISE EXCEPTION 'projection outbox release write returned an invalid result'
      USING ERRCODE='23514';
  END IF;
  INSERT INTO memory.relational_operation_request(
    owner_user_id,request_id,operation,target_key,manifest_sha256,
    outcome,result,invoked_by_session
  ) VALUES (
    actor,p_release_request_id,'release_projection_outbox_v1',
    p_outbox_id::text,p_release_manifest_sha256,'applied',
    expected_result,session_user
  );
  RETURN QUERY SELECT 'applied'::text,p_outbox_id,p_claim_id,
    p_expected_payload_sha256,p_release_manifest_sha256,2;
END
$function$;

ALTER FUNCTION memory.release_owner_projection_outbox_v1(uuid,uuid,uuid,text,text,text) OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.release_owner_projection_outbox_v1(uuid,uuid,uuid,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.release_owner_projection_outbox_v1(uuid,uuid,uuid,text,text,text) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.release_owner_projection_outbox_v1(uuid,uuid,uuid,text,text,text) TO memory_v5_writer;
