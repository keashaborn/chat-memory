BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regclass('memory.evidence') IS NULL THEN
    RAISE EXCEPTION 'V5 stage recorded-at compatibility prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.preflight_relational_stage_bundle_v5(
  p_evidence_id uuid,
  p_source_external_id text,
  p_source_sha256 text,
  p_source_recorded_at timestamptz
)
RETURNS TABLE(
  evidence_id uuid,
  verified boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  matched_id uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_evidence_id IS NULL
     OR btrim(COALESCE(p_source_external_id,''))=''
     OR NOT memory.v5_sha256_valid(p_source_sha256)
     OR p_source_recorded_at IS NULL THEN
    RAISE EXCEPTION 'V5 stage preflight inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT evidence.evidence_id INTO matched_id
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_evidence_id
    AND evidence.status='active'
    AND evidence.source_system='public.chat_log'
    AND evidence.external_id=p_source_external_id
    AND evidence.content_sha256=p_source_sha256
    AND evidence.recorded_at=p_source_recorded_at;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped stage evidence is unavailable'
      USING ERRCODE='P0002';
  END IF;
  RETURN QUERY SELECT matched_id,true;
END
$function$;

ALTER FUNCTION memory.preflight_relational_stage_bundle_v5(
  uuid,text,text,timestamptz
) OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.preflight_relational_stage_bundle_v5(
  uuid,text,text,timestamptz
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.preflight_relational_stage_bundle_v5(
  uuid,text,text,timestamptz
) TO brains_app;

COMMIT;
