\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE memory.project_thread_binding_event
  ADD CONSTRAINT project_thread_binding_event_owner_user_id_thread_id_fkey
    FOREIGN KEY (owner_user_id, thread_id)
    REFERENCES public.threads(owner_user_id, id)
    ON DELETE RESTRICT NOT VALID;
ALTER TABLE memory.project_thread_component_binding_event_v5
  ADD CONSTRAINT project_thread_component_binding_e_owner_user_id_thread_id_fkey
    FOREIGN KEY (owner_user_id, thread_id)
    REFERENCES public.threads(owner_user_id, id)
    ON DELETE RESTRICT NOT VALID;

ALTER TABLE memory.project_thread_binding_event
  VALIDATE CONSTRAINT project_thread_binding_event_owner_user_id_thread_id_fkey;
ALTER TABLE memory.project_thread_component_binding_event_v5
  VALIDATE CONSTRAINT project_thread_component_binding_e_owner_user_id_thread_id_fkey;

COMMIT;
