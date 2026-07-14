BEGIN;

CREATE OR REPLACE FUNCTION public.guard_canonical_owner()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.owner_user_id IS NULL THEN
      RAISE EXCEPTION '% requires owner_user_id', TG_TABLE_NAME
        USING ERRCODE = '23502';
    END IF;
    IF NEW.user_id IS NULL OR NEW.user_id <> NEW.owner_user_id::text THEN
      RAISE EXCEPTION '% owner_user_id/user_id mismatch', TG_TABLE_NAME
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
    RAISE EXCEPTION '% ownership is immutable', TG_TABLE_NAME
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION public.guard_chat_log_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id
     OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
     OR NEW.user_id IS DISTINCT FROM OLD.user_id
     OR NEW.source IS DISTINCT FROM OLD.source
     OR NEW.text IS DISTINCT FROM OLD.text
     OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'chat_log evidence fields are immutable'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS chat_log_guard_canonical_owner ON public.chat_log;
CREATE TRIGGER chat_log_guard_canonical_owner
BEFORE INSERT OR UPDATE OF owner_user_id, user_id
ON public.chat_log
FOR EACH ROW
EXECUTE FUNCTION public.guard_canonical_owner();

DROP TRIGGER IF EXISTS chat_log_guard_immutable ON public.chat_log;
CREATE TRIGGER chat_log_guard_immutable
BEFORE UPDATE ON public.chat_log
FOR EACH ROW
EXECUTE FUNCTION public.guard_chat_log_immutable();

DROP TRIGGER IF EXISTS threads_guard_canonical_owner ON public.threads;
CREATE TRIGGER threads_guard_canonical_owner
BEFORE INSERT OR UPDATE OF owner_user_id, user_id
ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.guard_canonical_owner();

ALTER TABLE public.chat_log
  VALIDATE CONSTRAINT chat_log_owner_matches_legacy_ck;
ALTER TABLE public.threads
  VALIDATE CONSTRAINT threads_owner_matches_legacy_ck;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'raw owner enforcement must run as sage, current_user=%', current_user;
  END IF;
  IF to_regprocedure('memory.current_actor_user_id()') IS NULL THEN
    RAISE EXCEPTION 'memory.current_actor_user_id() is required for raw RLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
END
$block$;

ALTER TABLE public.chat_log OWNER TO sage;
ALTER TABLE public.threads OWNER TO sage;

REVOKE ALL ON public.chat_log, public.threads FROM PUBLIC, brains_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.chat_log, public.threads TO brains_app;

ALTER TABLE public.chat_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS raw_owner_isolation ON public.chat_log;
CREATE POLICY raw_owner_isolation ON public.chat_log
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

ALTER TABLE public.threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.threads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS raw_owner_isolation ON public.threads;
CREATE POLICY raw_owner_isolation ON public.threads
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

REVOKE ALL ON FUNCTION public.guard_canonical_owner() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.guard_chat_log_immutable() FROM PUBLIC;

COMMIT;
