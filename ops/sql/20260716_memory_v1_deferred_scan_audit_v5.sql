BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'deferred scan audit migration requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure(
       'memory.scan_deferred_entailment_reconciliation_v5(integer)'
     ) IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL THEN
    RAISE EXCEPTION 'deferred scan audit prerequisites are absent';
  END IF;
END
$preflight$;

CREATE TABLE IF NOT EXISTS memory.deferred_reconciliation_scan_run_v5 (
  owner_user_id uuid NOT NULL,
  run_id uuid NOT NULL,
  scanner_version text NOT NULL,
  owner_roster_sha256 text NOT NULL,
  candidate_limit integer NOT NULL,
  candidate_count integer NOT NULL,
  candidates jsonb NOT NULL,
  candidates_sha256 text NOT NULL,
  run_manifest_sha256 text NOT NULL,
  worker_ref text NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id,run_id),
  CHECK (
    scanner_version='memory_v1_deferred_reconciliation_scanner_v5'
  ),
  CHECK (memory.v5_sha256_valid(owner_roster_sha256)),
  CHECK (candidate_limit BETWEEN 1 AND 100),
  CHECK (candidate_count BETWEEN 0 AND candidate_limit),
  CHECK (
    jsonb_typeof(candidates)='array'
    AND jsonb_array_length(candidates)=candidate_count
  ),
  CHECK (memory.v5_sha256_valid(candidates_sha256)),
  CHECK (
    memory.v5_digest_text(memory.v5_canonical_json_text(candidates))
      =candidates_sha256
  ),
  CHECK (memory.v5_sha256_valid(run_manifest_sha256)),
  CHECK (btrim(worker_ref)<>'' AND length(worker_ref)<=200)
);

CREATE INDEX IF NOT EXISTS deferred_scan_run_owner_created_idx
  ON memory.deferred_reconciliation_scan_run_v5(
    owner_user_id,created_at DESC,run_id
  );

CREATE OR REPLACE FUNCTION memory.run_deferred_reconciliation_scan_v5(
  p_run_id uuid,
  p_limit integer,
  p_owner_roster_sha256 text,
  p_worker_ref text
)
RETURNS TABLE(
  run_id uuid,
  outcome text,
  candidate_count integer,
  candidates_sha256 text,
  run_manifest_sha256 text,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  existing memory.deferred_reconciliation_scan_run_v5%ROWTYPE;
  candidates_value jsonb;
  candidates_hash text;
  run_manifest text;
  count_value integer;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_run_id IS NULL
     OR p_limit IS NULL OR p_limit<1 OR p_limit>100
     OR NOT memory.v5_sha256_valid(p_owner_roster_sha256)
     OR btrim(COALESCE(p_worker_ref,''))=''
     OR length(p_worker_ref)>200 THEN
    RAISE EXCEPTION 'deferred scan run inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|deferred_reconciliation_scan',0
  ));

  SELECT stored.* INTO existing
  FROM memory.deferred_reconciliation_scan_run_v5 AS stored
  WHERE stored.owner_user_id=actor
    AND stored.run_id=p_run_id;
  IF FOUND THEN
    IF existing.candidate_limit<>p_limit
       OR existing.owner_roster_sha256<>p_owner_roster_sha256
       OR existing.worker_ref<>btrim(p_worker_ref) THEN
      RAISE EXCEPTION 'deferred scan replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.run_id,'replayed',existing.candidate_count,
      existing.candidates_sha256,existing.run_manifest_sha256,0;
    RETURN;
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'claim_id',candidate.claim_id::text,
      'observation_id',candidate.observation_id::text,
      'decision_id',candidate.decision_id::text,
      'from_status',candidate.from_status,
      'target_status',candidate.target_status,
      'current_revision_number',candidate.current_revision_number,
      'support_count',candidate.support_count,
      'support_state_sha256',candidate.support_state_sha256,
      'review_authorization_manifest_sha256',
        candidate.review_authorization_manifest_sha256,
      'reconciliation_manifest_sha256',
        candidate.reconciliation_manifest_sha256
    )
    ORDER BY candidate.claim_id
  ),'[]'::jsonb)
  INTO candidates_value
  FROM memory.scan_deferred_entailment_reconciliation_v5(
    p_limit
  ) AS candidate;
  count_value := jsonb_array_length(candidates_value);
  candidates_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(candidates_value)
  );
  run_manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_deferred_reconciliation_scan_run_v5',
    actor::text,p_run_id::text,p_limit::text,
    p_owner_roster_sha256,candidates_hash,
    count_value::text,btrim(p_worker_ref)
  ));

  INSERT INTO memory.deferred_reconciliation_scan_run_v5(
    owner_user_id,run_id,scanner_version,owner_roster_sha256,
    candidate_limit,candidate_count,candidates,candidates_sha256,
    run_manifest_sha256,worker_ref,invoked_by_session
  ) VALUES (
    actor,p_run_id,'memory_v1_deferred_reconciliation_scanner_v5',
    p_owner_roster_sha256,p_limit,count_value,candidates_value,
    candidates_hash,run_manifest,btrim(p_worker_ref),session_user
  );
  RETURN QUERY SELECT
    p_run_id,'applied',count_value,candidates_hash,run_manifest,1;
END
$function$;

ALTER TABLE memory.deferred_reconciliation_scan_run_v5
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.run_deferred_reconciliation_scan_v5(
  uuid,integer,text,text
) OWNER TO memory_v5_writer;

ALTER TABLE memory.deferred_reconciliation_scan_run_v5
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.deferred_reconciliation_scan_run_v5
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.deferred_reconciliation_scan_run_v5;
CREATE POLICY owner_isolation
  ON memory.deferred_reconciliation_scan_run_v5
  TO memory_v5_writer
  USING (
    owner_user_id=(SELECT memory.current_actor_user_id())
  )
  WITH CHECK (
    owner_user_id=(SELECT memory.current_actor_user_id())
  );

DROP TRIGGER IF EXISTS deferred_reconciliation_scan_run_v5_append_only_guard
  ON memory.deferred_reconciliation_scan_run_v5;
CREATE TRIGGER deferred_reconciliation_scan_run_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.deferred_reconciliation_scan_run_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

GRANT SELECT,INSERT ON memory.deferred_reconciliation_scan_run_v5
  TO memory_v5_writer;
REVOKE ALL ON memory.deferred_reconciliation_scan_run_v5
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.run_deferred_reconciliation_scan_v5(
  uuid,integer,text,text
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.run_deferred_reconciliation_scan_v5(
  uuid,integer,text,text
) TO brains_app;

COMMIT;
