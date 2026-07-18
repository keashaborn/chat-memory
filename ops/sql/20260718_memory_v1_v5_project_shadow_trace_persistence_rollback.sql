BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 project shadow trace rollback requires sage';
  END IF;
  IF to_regclass('memory.v5_project_shadow_trace_event') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.v5_project_shadow_trace_event LIMIT 1
     ) THEN
    RAISE EXCEPTION 'project shadow trace rows exist; rollback is unsafe';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.record_v5_project_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,jsonb,integer,integer
);
DROP TABLE IF EXISTS memory.v5_project_shadow_trace_event;
DROP FUNCTION IF EXISTS memory.guard_v5_project_shadow_trace_append_only();

COMMIT;
