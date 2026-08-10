\set ON_ERROR_STOP on

-- Disposable clean-successor cluster only. No production identity, data,
-- membership, database, extension, or credential is copied into this fixture.
CREATE ROLE governed_memory_owner
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;
CREATE ROLE governed_memory_api
  LOGIN PASSWORD 'successor_api_disposable_only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;
CREATE ROLE governed_memory_worker
  LOGIN PASSWORD 'successor_worker_disposable_only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;
CREATE ROLE memory_ingest_writer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;
-- Production owns the conversation tables and bridge definer functions with
-- the existing sage superuser. The disposable fixture mirrors that boundary.
CREATE ROLE sage
  LOGIN PASSWORD 'successor_sage_disposable_only'
  SUPERUSER CREATEDB CREATEROLE NOREPLICATION BYPASSRLS INHERIT;
CREATE ROLE brains_app
  LOGIN PASSWORD 'successor_brains_app_disposable_only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS INHERIT;

CREATE DATABASE governed_memory
  WITH OWNER governed_memory_owner ENCODING 'UTF8' TEMPLATE template0;
COMMENT ON DATABASE governed_memory IS
  'governed-memory-successor-disposable:019fe927';
REVOKE ALL ON DATABASE governed_memory FROM PUBLIC;
GRANT CONNECT ON DATABASE governed_memory
  TO governed_memory_api, governed_memory_worker;

CREATE DATABASE memory
  WITH OWNER sage ENCODING 'UTF8' TEMPLATE template0;
COMMENT ON DATABASE memory IS
  'governed-memory-successor-disposable:019fe927';
REVOKE ALL ON DATABASE memory FROM PUBLIC;
GRANT CONNECT ON DATABASE memory TO brains_app, governed_memory_worker;

\connect governed_memory
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION pgcrypto WITH SCHEMA public;

\connect memory
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA memory AUTHORIZATION sage;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO brains_app;

CREATE FUNCTION memory.current_actor_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
$function$;
REVOKE ALL ON FUNCTION memory.current_actor_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;

CREATE TABLE public.threads (
  id uuid PRIMARY KEY,
  user_id text NOT NULL,
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
  archived boolean NOT NULL DEFAULT false,
  owner_user_id uuid,
  title_source text NOT NULL DEFAULT 'placeholder'
    CHECK (title_source IN ('placeholder', 'automatic', 'manual')),
  pinned_at timestamptz,
  CONSTRAINT threads_owner_matches_legacy_ck CHECK (
    owner_user_id IS NULL OR user_id = owner_user_id::text
  ),
  UNIQUE (owner_user_id, id),
  UNIQUE (id, owner_user_id)
);

CREATE TABLE public.chat_log (
  id uuid PRIMARY KEY,
  user_id text,
  source text,
  text text,
  tags text[],
  created_at timestamptz,
  thread_id uuid,
  vantage_id text,
  user_id_alias text,
  request_id text,
  owner_user_id uuid,
  CONSTRAINT chat_log_owner_matches_legacy_ck CHECK (
    owner_user_id IS NULL OR user_id = owner_user_id::text
  ),
  CONSTRAINT chat_log_id_owner_thread_chat_attachments_uq
    UNIQUE (id, owner_user_id, thread_id)
);
ALTER TABLE public.chat_log
  ADD CONSTRAINT chat_log_owner_thread_fk
  FOREIGN KEY (owner_user_id, thread_id)
  REFERENCES public.threads(owner_user_id, id) NOT VALID;
CREATE INDEX chat_log_owner_thread_time_idx
  ON public.chat_log(owner_user_id, thread_id, created_at DESC)
  WHERE owner_user_id IS NOT NULL;

CREATE TABLE public.chat_attachments (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  message_id uuid,
  filename text NOT NULL,
  media_type text NOT NULL,
  content text,
  content_sha256 text NOT NULL,
  byte_size integer NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
  deleted_at timestamptz,
  FOREIGN KEY (thread_id, owner_user_id)
    REFERENCES public.threads(id, owner_user_id) ON DELETE CASCADE,
  FOREIGN KEY (message_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id) ON DELETE CASCADE
);

CREATE FUNCTION public.guard_canonical_owner()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO pg_catalog, public
AS $function$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.owner_user_id IS NULL OR NEW.user_id IS NULL
       OR NEW.user_id <> NEW.owner_user_id::text THEN
      RAISE EXCEPTION '% requires matching canonical owner', TG_TABLE_NAME
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

CREATE FUNCTION public.guard_chat_log_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO pg_catalog, public
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

CREATE FUNCTION memory.enqueue_chat_log_consolidation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'legacy fixture trigger must remain disabled';
END
$function$;

CREATE TRIGGER chat_log_guard_canonical_owner
BEFORE INSERT OR UPDATE OF owner_user_id, user_id ON public.chat_log
FOR EACH ROW EXECUTE FUNCTION public.guard_canonical_owner();
CREATE TRIGGER chat_log_guard_immutable
BEFORE UPDATE ON public.chat_log
FOR EACH ROW EXECUTE FUNCTION public.guard_chat_log_immutable();
CREATE TRIGGER threads_guard_canonical_owner
BEFORE INSERT OR UPDATE OF owner_user_id, user_id ON public.threads
FOR EACH ROW EXECUTE FUNCTION public.guard_canonical_owner();
CREATE TRIGGER chat_log_enqueue_memory_v1_consolidation
AFTER INSERT ON public.chat_log
FOR EACH ROW EXECUTE FUNCTION memory.enqueue_chat_log_consolidation();
ALTER TABLE public.chat_log
  DISABLE TRIGGER chat_log_enqueue_memory_v1_consolidation;

ALTER TABLE public.chat_log OWNER TO sage;
ALTER TABLE public.threads OWNER TO sage;
ALTER TABLE public.chat_attachments OWNER TO sage;
ALTER FUNCTION memory.current_actor_user_id() OWNER TO sage;
ALTER FUNCTION public.guard_canonical_owner() OWNER TO sage;
ALTER FUNCTION public.guard_chat_log_immutable() OWNER TO sage;
ALTER FUNCTION memory.enqueue_chat_log_consolidation() OWNER TO sage;
REVOKE ALL ON public.chat_log, public.threads, public.chat_attachments
  FROM PUBLIC, brains_app, memory_ingest_writer, governed_memory_worker;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON public.chat_log, public.threads, public.chat_attachments TO brains_app;

ALTER TABLE public.chat_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_log FORCE ROW LEVEL SECURITY;
CREATE POLICY raw_owner_isolation ON public.chat_log
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());
ALTER TABLE public.threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.threads FORCE ROW LEVEL SECURITY;
CREATE POLICY raw_owner_isolation ON public.threads
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());
ALTER TABLE public.chat_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_attachments FORCE ROW LEVEL SECURITY;
CREATE POLICY chat_attachments_owner_isolation ON public.chat_attachments
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

REVOKE ALL ON FUNCTION public.guard_canonical_owner() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.guard_chat_log_immutable() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.enqueue_chat_log_consolidation() FROM PUBLIC;
