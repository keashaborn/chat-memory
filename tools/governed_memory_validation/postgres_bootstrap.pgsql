\set ON_ERROR_STOP on

-- Disposable clean-successor cluster only. This provisions no production identity,
-- data, membership, database, extension, or credential.
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
CREATE ROLE sage
  LOGIN PASSWORD 'successor_sage_disposable_only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;
CREATE ROLE successor_ingest_login
  LOGIN PASSWORD 'successor_ingest_disposable_only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT;

GRANT memory_ingest_writer TO successor_ingest_login;

CREATE DATABASE governed_memory
  WITH OWNER governed_memory_owner ENCODING 'UTF8' TEMPLATE template0;
COMMENT ON DATABASE governed_memory IS
  'governed-memory-successor-disposable:019fe927';
REVOKE ALL ON DATABASE governed_memory FROM PUBLIC;
GRANT CONNECT ON DATABASE governed_memory
  TO governed_memory_api, governed_memory_worker;

CREATE DATABASE successor_conversation
  WITH OWNER sage ENCODING 'UTF8' TEMPLATE template0;
COMMENT ON DATABASE successor_conversation IS
  'governed-memory-successor-disposable:019fe927';
REVOKE ALL ON DATABASE successor_conversation FROM PUBLIC;
GRANT CONNECT ON DATABASE successor_conversation
  TO governed_memory_worker, successor_ingest_login;

\connect governed_memory
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION pgcrypto WITH SCHEMA public;

\connect successor_conversation
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Disposable stand-in for the canonical conversation reader. It proves that a
-- restarted extraction worker resolves source/context by immutable IDs and
-- hashes instead of retaining an in-process eligibility object.
CREATE TABLE public.successor_conversation_message_fixture (
  owner_user_id uuid NOT NULL,
  message_id uuid PRIMARY KEY,
  thread_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL,
  UNIQUE (owner_user_id, message_id)
);
ALTER TABLE public.successor_conversation_message_fixture OWNER TO sage;
REVOKE ALL ON public.successor_conversation_message_fixture FROM PUBLIC;
GRANT SELECT ON public.successor_conversation_message_fixture
  TO governed_memory_worker;
