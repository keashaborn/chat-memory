BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'active thread selection migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION
      'memory.current_actor_user_id() is required for active-thread RLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'threads'
      AND indexname = 'threads_owner_id_uq'
  ) THEN
    RAISE EXCEPTION 'threads_owner_id_uq is required';
  END IF;
END
$block$;

CREATE TABLE IF NOT EXISTS public.active_thread_selection (
  owner_user_id uuid PRIMARY KEY,
  thread_id uuid,
  selected_at timestamp with time zone,
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT active_thread_selection_state_ck CHECK (
    (thread_id IS NULL AND selected_at IS NULL)
    OR
    (thread_id IS NOT NULL AND selected_at IS NOT NULL)
  ),
  CONSTRAINT active_thread_selection_owner_thread_fk
    FOREIGN KEY (owner_user_id, thread_id)
    REFERENCES public.threads(owner_user_id, id)
    ON DELETE CASCADE
);

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.active_thread_selection'::regclass
      AND conname = 'active_thread_selection_state_ck'
      AND contype = 'c'
  ) THEN
    RAISE EXCEPTION 'active_thread_selection_state_ck is required';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'public.active_thread_selection'::regclass
      AND conname = 'active_thread_selection_owner_thread_fk'
      AND contype = 'f'
  ) THEN
    RAISE EXCEPTION
      'active_thread_selection_owner_thread_fk is required';
  END IF;
END
$block$;

COMMENT ON TABLE public.active_thread_selection IS
  'Server-authoritative active conversation per verified Supabase user';
COMMENT ON COLUMN public.active_thread_selection.thread_id IS
  'NULL records an explicit new-chat/cleared state';

CREATE INDEX IF NOT EXISTS active_thread_selection_thread_idx
  ON public.active_thread_selection(thread_id)
  WHERE thread_id IS NOT NULL;

ALTER TABLE public.active_thread_selection OWNER TO sage;
REVOKE ALL ON public.active_thread_selection FROM PUBLIC, brains_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON public.active_thread_selection TO brains_app;

ALTER TABLE public.active_thread_selection ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.active_thread_selection FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS active_thread_owner_isolation
  ON public.active_thread_selection;
CREATE POLICY active_thread_owner_isolation
  ON public.active_thread_selection
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

COMMIT;
