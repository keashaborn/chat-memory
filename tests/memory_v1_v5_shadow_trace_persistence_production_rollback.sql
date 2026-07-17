\set ON_ERROR_STOP on

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR (SELECT count(*) FROM memory.v5_shadow_trace_event)<>0 THEN
    RAISE EXCEPTION 'production rollback probe requires sage and an empty trace table';
  END IF;
END
$preflight$;

SET SESSION AUTHORIZATION brains_app;
BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);
CREATE TEMP TABLE owner_a ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('4',64),'skipped','no_governed_claim_route',
  NULL,NULL,repeat('5',64),repeat('5',64),0,0,0,0,'{}'::jsonb,
  24,4,500,'medium',0
);
CREATE TEMP TABLE owner_a_replay ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('2',64),
  repeat('3',64),repeat('4',64),'skipped','no_governed_claim_route',
  NULL,NULL,repeat('5',64),repeat('5',64),0,0,0,0,'{}'::jsonb,
  24,4,500,'medium',0
);
SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
CREATE TEMP TABLE owner_b ON COMMIT DROP AS
SELECT * FROM memory.record_v5_shadow_trace_v1(
  'memory_v1_v5_shadow_trace_v1',repeat('1',64),repeat('6',64),
  repeat('7',64),repeat('8',64),'skipped','turn_intent:tech',
  NULL,NULL,repeat('9',64),repeat('9',64),0,0,0,0,'{}'::jsonb,
  24,4,500,'medium',0
);
DO $checks$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM owner_a WHERE outcome='applied' AND rows_written=1)
     OR NOT EXISTS (SELECT 1 FROM owner_a_replay WHERE outcome='replayed' AND rows_written=0)
     OR NOT EXISTS (SELECT 1 FROM owner_b WHERE outcome='applied' AND rows_written=1) THEN
    RAISE EXCEPTION 'production rollback apply/replay/isolation probe failed';
  END IF;
  BEGIN
    PERFORM count(*) FROM memory.v5_shadow_trace_event;
    RAISE EXCEPTION 'brains_app acquired direct trace-table access';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$checks$;
ROLLBACK;
RESET SESSION AUTHORIZATION;

DO $postflight$
BEGIN
  IF (SELECT count(*) FROM memory.v5_shadow_trace_event)<>0 THEN
    RAISE EXCEPTION 'production rollback probe left trace rows';
  END IF;
END
$postflight$;

SELECT 'memory_v1_v5_shadow_trace_persistence_production_rollback: PASS' AS result;
