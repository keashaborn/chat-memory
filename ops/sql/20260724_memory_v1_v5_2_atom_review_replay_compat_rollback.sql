BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom review replay rollback requires sage';
  END IF;
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure(
       'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom review replay rollback prerequisites absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  p_operation_id uuid,
  p_review_id uuid,
  p_proposal_id uuid,
  p_decision memory.v5_2_atom_review_decision,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_review_reason_codes jsonb,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  review_id uuid,
  outcome text,
  review_number integer,
  decision memory.v5_2_atom_review_decision
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  replay memory.v5_2_atom_admission_operation%ROWTYPE;
  operation_manifest text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_operation_id IS NULL OR p_review_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'atom review request inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'v5_2_atom_review',p_proposal_id::text),0
  ));
  operation_manifest:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_review_record_v1',actor::text,
    p_review_id::text,p_proposal_id::text,
    p_authorization_manifest_sha256
  ));

  SELECT * INTO replay
  FROM memory.v5_2_atom_admission_operation
  WHERE owner_user_id=actor AND operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.operation<>'record_review'
       OR replay.manifest_sha256<>operation_manifest THEN
      RAISE EXCEPTION 'atom review replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replay.result->>'review_id')::uuid,'replayed',
      (replay.result->>'review_number')::integer,
      (replay.result->>'decision')::memory.v5_2_atom_review_decision;
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_owner_v5_2_atom_review_v1(
    p_proposal_id,p_decision,p_reviewer_type,p_reviewer_ref,
    p_review_reason_codes
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'atom review authorization manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_atom_admission_review(
    review_id,owner_user_id,operation_id,proposal_id,review_number,
    prior_review_id,decision,reviewer_type,reviewer_ref,
    review_reason_codes,proposal_sha256,authorization_manifest_sha256
  ) VALUES (
    p_review_id,actor,p_operation_id,p_proposal_id,
    preflight.next_review_number,preflight.prior_review_id,p_decision,
    p_reviewer_type,p_reviewer_ref,p_review_reason_codes,
    preflight.proposal_sha256,p_authorization_manifest_sha256
  );
  result_value:=jsonb_build_object(
    'review_id',p_review_id::text,
    'review_number',preflight.next_review_number,
    'decision',p_decision::text
  );
  INSERT INTO memory.v5_2_atom_admission_operation(
    owner_user_id,operation_id,operation,manifest_sha256,result,
    invoked_by_session
  ) VALUES (
    actor,p_operation_id,'record_review',operation_manifest,
    result_value,session_user
  );
  RETURN QUERY SELECT
    p_review_id,'applied',preflight.next_review_number,p_decision;
END
$function$;

ALTER FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) TO brains_app;

COMMENT ON FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) IS 'Records one append-only V5.2 atom review under a current authorization manifest.';

COMMIT;
