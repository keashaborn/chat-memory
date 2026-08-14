\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

DO $preflight$
BEGIN
  IF current_database() <> 'memory' OR current_user <> 'sage' THEN
    RAISE EXCEPTION 'legacy chat dependency detach identity mismatch';
  END IF;
  IF (
    SELECT count(*)
    FROM pg_constraint AS constraint_row
    WHERE constraint_row.contype = 'f'
      AND constraint_row.convalidated
      AND constraint_row.confrelid = 'public.threads'::regclass
      AND (
        (constraint_row.conrelid = 'memory.project_thread_binding_event'::regclass
         AND constraint_row.conname = 'project_thread_binding_event_owner_user_id_thread_id_fkey')
        OR
        (constraint_row.conrelid = 'memory.project_thread_component_binding_event_v5'::regclass
         AND constraint_row.conname = 'project_thread_component_binding_e_owner_user_id_thread_id_fkey')
      )
  ) <> 2 THEN
    RAISE EXCEPTION 'legacy project thread constraints differ';
  END IF;
END;
$preflight$;

ALTER TABLE memory.project_thread_binding_event
  DROP CONSTRAINT project_thread_binding_event_owner_user_id_thread_id_fkey;
ALTER TABLE memory.project_thread_component_binding_event_v5
  DROP CONSTRAINT project_thread_component_binding_e_owner_user_id_thread_id_fkey;

COMMIT;
