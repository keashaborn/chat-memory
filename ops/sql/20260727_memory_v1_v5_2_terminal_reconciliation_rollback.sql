\set ON_ERROR_STOP on
BEGIN;

DO $circuit_rollback$
DECLARE
  function_oid constant regprocedure :=
    'memory.owner_v5_local_inference_circuit_state_v2(text,text,text,text,text,text,integer,integer)'::regprocedure;
  expected_before constant text :=
    '2a448cc3220259ad0168e9c51b85bd66395df2209073f0402ac8a6e3fba3dc3e';
  failure_anchor constant text :=
    '    AND classification.circuit_impact' || E'\n' ||
    '    AND (';
  failure_replacement constant text :=
    '    AND classification.circuit_impact' || E'\n' ||
    '    AND NOT EXISTS (' || E'\n' ||
    '      SELECT 1' || E'\n' ||
    '      FROM memory.v5_local_inference_outcome_supersession_v1 AS supersession' || E'\n' ||
    '      WHERE supersession.owner_user_id=actor' || E'\n' ||
    '        AND supersession.original_event_id=completed.event_id' || E'\n' ||
    '    )' || E'\n' ||
    '    AND (';
  immediate_anchor constant text :=
    '      AND classification.immediate_open' || E'\n' ||
    '      AND (';
  immediate_replacement constant text :=
    '      AND classification.immediate_open' || E'\n' ||
    '      AND NOT EXISTS (' || E'\n' ||
    '        SELECT 1' || E'\n' ||
    '        FROM memory.v5_local_inference_outcome_supersession_v1 AS supersession' || E'\n' ||
    '        WHERE supersession.owner_user_id=actor' || E'\n' ||
    '          AND supersession.original_event_id=completed.event_id' || E'\n' ||
    '      )' || E'\n' ||
    '      AND (';
  source text;
  source_sha text;
  restored_sha text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  source_sha := encode(public.digest(
    convert_to(source, 'UTF8'), 'sha256'
  ), 'hex');
  IF source_sha <> expected_before THEN
    RAISE EXCEPTION 'terminal reconciliation rollback baseline changed: %',
      source_sha USING ERRCODE = '23514';
  END IF;
  IF (length(source) - length(replace(source, failure_replacement, '')))
       / length(failure_replacement) <> 1
     OR (
       length(source) - length(replace(source, immediate_replacement, ''))
     ) / length(immediate_replacement) <> 1
  THEN
    RAISE EXCEPTION 'terminal reconciliation rollback anchor changed'
      USING ERRCODE = '23514';
  END IF;
  source := replace(source, failure_replacement, failure_anchor);
  source := replace(source, immediate_replacement, immediate_anchor);
  EXECUTE source;

  SELECT encode(public.digest(convert_to(pg_get_functiondef(function_oid),
    'UTF8'), 'sha256'), 'hex')
  INTO restored_sha;
  IF restored_sha <>
     'e207e9376d0b0df19059645be66777aae18e8abb20b565313601b685ff4de899'
  THEN
    RAISE EXCEPTION 'terminal reconciliation circuit rollback mismatch: %',
      restored_sha USING ERRCODE = '23514';
  END IF;
END
$circuit_rollback$;

REVOKE EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_inference_supersession_v1(),
  memory.plan_owner_v5_local_terminal_reconciliation_v1(integer),
  memory.apply_owner_v5_local_inference_supersession_v1(
    uuid, uuid, uuid, uuid, text, text, text
  ),
  memory.finalize_owner_v5_local_terminal_reconciliation_v1(
    uuid, uuid, text, text, text
  )
FROM brains_app;

COMMIT;
