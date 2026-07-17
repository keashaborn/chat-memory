BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 shadow trace persistence rollback requires sage';
  END IF;
END
$preflight$;

REVOKE ALL ON FUNCTION memory.record_v5_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer
) FROM PUBLIC,brains_app;
DROP FUNCTION IF EXISTS memory.record_v5_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,integer,jsonb,integer,integer,integer,text,integer
);
DROP TABLE IF EXISTS memory.v5_shadow_trace_event;
DROP FUNCTION IF EXISTS memory.require_v5_shadow_trace_writer_context();
DROP FUNCTION IF EXISTS memory.guard_v5_shadow_trace_append_only();
DROP FUNCTION IF EXISTS memory.v5_shadow_rejection_counts_valid(jsonb);
DROP FUNCTION IF EXISTS memory.v5_shadow_trace_sha256_valid(text);

REVOKE EXECUTE ON FUNCTION memory.current_actor_user_id()
  FROM memory_v5_trace_writer;
REVOKE USAGE ON SCHEMA memory FROM memory_v5_trace_writer;

DO $drop_role$
BEGIN
  IF to_regrole('memory_v5_trace_writer') IS NOT NULL THEN
    DROP ROLE memory_v5_trace_writer;
  END IF;
END
$drop_role$;

COMMIT;
