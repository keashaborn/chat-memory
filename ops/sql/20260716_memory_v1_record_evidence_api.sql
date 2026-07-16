BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'record evidence API migration requires sage';
  END IF;
  IF to_regrole('memory_evidence_maintainer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_evidence_insert_only()') IS NULL
     OR to_regclass('memory.evidence') IS NULL THEN
    RAISE EXCEPTION 'record evidence API prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.record_owner_evidence_v1(
  p_kind memory.evidence_kind,
  p_source_system text,
  p_external_id text,
  p_content text,
  p_observed_at timestamptz,
  p_directness numeric,
  p_source_reliability numeric,
  p_independence_key text,
  p_sensitivity memory.sensitivity_level,
  p_metadata jsonb
)
RETURNS TABLE(
  evidence_id uuid,
  outcome text,
  content_sha256 text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  content_hash text;
  existing memory.evidence%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'record evidence API requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_kind IS NULL
     OR btrim(COALESCE(p_source_system,''))=''
     OR length(p_source_system)>200
     OR btrim(COALESCE(p_external_id,''))=''
     OR length(p_external_id)>500
     OR p_sensitivity IS NULL
     OR p_metadata IS NULL
     OR jsonb_typeof(p_metadata)<>'object'
     OR pg_column_size(p_metadata)>65536
     OR (p_directness IS NOT NULL
         AND (p_directness<0 OR p_directness>1))
     OR (p_source_reliability IS NOT NULL
         AND (p_source_reliability<0 OR p_source_reliability>1)) THEN
    RAISE EXCEPTION 'record evidence inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  content_hash := CASE
    WHEN p_content IS NULL THEN NULL
    ELSE encode(
      public.digest(convert_to(p_content,'UTF8'),'sha256'),
      'hex'
    )
  END;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'record_owner_evidence_v1',
      p_source_system,p_external_id),
    0
  ));

  SELECT evidence.* INTO existing
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.source_system=p_source_system
    AND evidence.external_id=p_external_id;
  IF FOUND THEN
    IF existing.status IN ('redacted','deleted') THEN
      RAISE EXCEPTION
        'evidence external_id is tombstoned and cannot be reinserted'
        USING ERRCODE='23514';
    END IF;
    IF existing.content_sha256 IS DISTINCT FROM content_hash THEN
      RAISE EXCEPTION
        'evidence external_id already exists with different content'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.evidence_id,'replayed',existing.content_sha256;
    RETURN;
  END IF;

  INSERT INTO memory.evidence AS inserted(
    owner_user_id,kind,source_system,external_id,content,
    content_sha256,observed_at,directness,source_reliability,
    independence_key,sensitivity,metadata
  ) VALUES (
    actor,p_kind,p_source_system,p_external_id,p_content,
    content_hash,p_observed_at,p_directness,p_source_reliability,
    p_independence_key,p_sensitivity,p_metadata
  )
  RETURNING inserted.evidence_id,
            inserted.content_sha256
  INTO existing.evidence_id,existing.content_sha256;

  RETURN QUERY SELECT
    existing.evidence_id,'applied',existing.content_sha256;
END
$function$;

GRANT INSERT ON memory.evidence TO memory_evidence_maintainer;
ALTER FUNCTION memory.record_owner_evidence_v1(
  memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,
  memory.sensitivity_level,jsonb
) OWNER TO memory_evidence_maintainer;

REVOKE ALL ON FUNCTION memory.record_owner_evidence_v1(
  memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,
  memory.sensitivity_level,jsonb
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_evidence_v1(
  memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,
  memory.sensitivity_level,jsonb
) TO brains_app;

COMMIT;
