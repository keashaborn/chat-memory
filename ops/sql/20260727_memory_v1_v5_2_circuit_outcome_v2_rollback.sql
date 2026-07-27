BEGIN;

DO $rollback$
DECLARE
  function_oid constant regprocedure :=
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure;
  old_declarations constant text :=
    '  rejection_count integer;' || E'\n' ||
    '  block_reason text;' || E'\n' ||
    '  circuit record;';
  new_declarations constant text :=
    '  latest_count integer;' || E'\n' ||
    '  latest_all_rejected boolean;' || E'\n' ||
    '  rejection_count integer;' || E'\n' ||
    '  block_reason text;';
  old_control constant text :=
    '  SELECT * INTO circuit' || E'\n' ||
    '  FROM memory.owner_v5_local_inference_circuit_state_v2(' || E'\n' ||
    '    p_provider_id,p_provider_version,p_provider_model_sha256,' || E'\n' ||
    '    p_model_file_sha256,p_runtime_revision_sha256,' || E'\n' ||
    '    p_policy_compiler_sha256,p_failure_threshold,900' || E'\n' ||
    '  );' || E'\n' ||
    '  rejection_count:=least(circuit.systemic_failure_count,10);' || E'\n' ||
    '  IF circuit.circuit_state IN (''open'',''half_open_inflight'') THEN' || E'\n' ||
    '    block_reason := ''circuit_open'';' || E'\n' ||
    '  ELSIF reserved_count>=p_max_reserved_jobs THEN' || E'\n' ||
    '    block_reason := ''quota_exhausted'';' || E'\n' ||
    '  END IF;';
  new_control constant text :=
    '  SELECT count(*)::integer,coalesce(bool_and(recent.outcome=''rejected''),false)' || E'\n' ||
    '  INTO latest_count,latest_all_rejected' || E'\n' ||
    '  FROM (' || E'\n' ||
    '    SELECT event.outcome' || E'\n' ||
    '    FROM memory.v5_local_inference_event AS event' || E'\n' ||
    '    WHERE event.owner_user_id=actor AND event.action=''completed''' || E'\n' ||
    '      AND event.provider_id=p_provider_id' || E'\n' ||
    '      AND event.provider_version=p_provider_version' || E'\n' ||
    '      AND event.provider_model_sha256=p_provider_model_sha256' || E'\n' ||
    '      AND event.model_file_sha256=p_model_file_sha256' || E'\n' ||
    '      AND event.runtime_revision_sha256=p_runtime_revision_sha256' || E'\n' ||
    '      AND event.policy_compiler_sha256=p_policy_compiler_sha256' || E'\n' ||
    '      AND event.rejection_code IS DISTINCT FROM ''local_worker_abandoned''' || E'\n' ||
    '    ORDER BY event.created_at DESC,event.event_id DESC' || E'\n' ||
    '    LIMIT p_failure_threshold' || E'\n' ||
    '  ) AS recent;' || E'\n' ||
    '  rejection_count := CASE' || E'\n' ||
    '    WHEN latest_count=p_failure_threshold AND latest_all_rejected' || E'\n' ||
    '      THEN p_failure_threshold ELSE 0' || E'\n' ||
    '  END;' || E'\n' ||
    '  IF reserved_count>=p_max_reserved_jobs THEN' || E'\n' ||
    '    block_reason := ''quota_exhausted'';' || E'\n' ||
    '  ELSIF rejection_count>=p_failure_threshold THEN' || E'\n' ||
    '    block_reason := ''circuit_open'';' || E'\n' ||
    '  END IF;';
  source text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  IF (length(source)-length(replace(source,old_declarations,'')))
       / length(old_declarations)<>1
     OR (length(source)-length(replace(source,old_control,'')))
       / length(old_control)<>1 THEN
    RAISE EXCEPTION 'local outcome circuit rollback anchor changed'
      USING ERRCODE='23514';
  END IF;
  source:=replace(source,old_declarations,new_declarations);
  source:=replace(source,old_control,new_control);
  EXECUTE source;
END
$rollback$;

ALTER FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) OWNER TO memory_v5_local_inference_maintainer;
REVOKE ALL ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION memory.claim_owner_v5_local_inference_job_v1(
  uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,
  integer,integer,integer
) TO brains_app;

DROP FUNCTION memory.owner_v5_local_inference_status_v1(
  integer,text,text,text,text,text,text,integer,integer,integer,integer
);
DROP FUNCTION memory.owner_v5_local_inference_circuit_state_v2(
  text,text,text,text,text,text,integer,integer
);
DROP FUNCTION memory.finalize_owner_v5_local_record_outcome_v2(
  uuid,uuid,uuid,text,text,uuid,text
);
DO $outcome_history$
BEGIN
  REVOKE ALL ON memory.v5_local_inference_outcome_event
    FROM PUBLIC,brains_app,memory_v5_local_inference_maintainer;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_inference_outcome_event LIMIT 1
  ) THEN
    COMMENT ON TABLE memory.v5_local_inference_outcome_event IS
      'Dormant append-only V5 local outcome history retained by rollback.';
  ELSE
    DROP TABLE memory.v5_local_inference_outcome_event;
  END IF;
END
$outcome_history$;
DROP FUNCTION memory.classify_v5_local_inference_outcome_v2(text);

COMMIT;
