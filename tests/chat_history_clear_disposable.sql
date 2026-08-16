\set ON_ERROR_STOP on

CREATE ROLE sage NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER DATABASE postgres OWNER TO sage;
SET ROLE sage;

CREATE SCHEMA memory_ingest_private AUTHORIZATION sage;
CREATE SCHEMA trusted_web AUTHORIZATION sage;
CREATE SCHEMA chat_integrity AUTHORIZATION sage;

CREATE TABLE public.threads (
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  title text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id),
  UNIQUE (owner_user_id, id)
);
CREATE TABLE public.chat_log (
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  text text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id),
  UNIQUE (id, owner_user_id, thread_id),
  FOREIGN KEY (owner_user_id, thread_id)
    REFERENCES public.threads(owner_user_id, id)
);
CREATE TABLE public.chat_attachments (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  message_id uuid NOT NULL,
  FOREIGN KEY (message_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE
);
CREATE TABLE public.active_thread_selection (
  owner_user_id uuid PRIMARY KEY,
  thread_id uuid NOT NULL,
  FOREIGN KEY (owner_user_id, thread_id)
    REFERENCES public.threads(owner_user_id, id)
    ON DELETE CASCADE
);
CREATE TABLE trusted_web.response_transcript_v1 (
  response_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  user_chat_log_id uuid NOT NULL,
  assistant_chat_log_id uuid NOT NULL,
  FOREIGN KEY (user_chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE,
  FOREIGN KEY (assistant_chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE
);
CREATE TABLE chat_integrity.assistant_transcript_attestation_v1 (
  answer_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  chat_log_id uuid NOT NULL,
  FOREIGN KEY (chat_log_id, owner_user_id, thread_id)
    REFERENCES public.chat_log(id, owner_user_id, thread_id)
    ON DELETE CASCADE
);
CREATE TABLE memory_ingest_private.memory_ingest_outbox (
  outbox_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  message_id uuid NOT NULL,
  state text NOT NULL
);

INSERT INTO public.threads(id, owner_user_id, title) VALUES
  ('10000000-0000-4000-8000-000000000001', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'owner'),
  ('20000000-0000-4000-8000-000000000001', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'other');
INSERT INTO public.chat_log(id, owner_user_id, thread_id, text) VALUES
  ('10000000-0000-4000-8000-000000000011', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', 'owner user'),
  ('10000000-0000-4000-8000-000000000012', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', 'owner assistant'),
  ('20000000-0000-4000-8000-000000000011', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '20000000-0000-4000-8000-000000000001', 'other user');
INSERT INTO memory_ingest_private.memory_ingest_outbox(
  outbox_id, owner_user_id, message_id, state
) VALUES
  ('10000000-0000-4000-8000-000000000021', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000011', 'completed'),
  ('20000000-0000-4000-8000-000000000021', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '20000000-0000-4000-8000-000000000011', 'completed');
INSERT INTO public.active_thread_selection(owner_user_id, thread_id) VALUES
  ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001');
INSERT INTO chat_integrity.assistant_transcript_attestation_v1(
  answer_id, owner_user_id, thread_id, chat_log_id
) VALUES (
  '10000000-0000-4000-8000-000000000031',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000012'
);

ALTER TABLE public.threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.threads FORCE ROW LEVEL SECURITY;
ALTER TABLE public.chat_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_log FORCE ROW LEVEL SECURITY;
ALTER TABLE public.chat_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_attachments FORCE ROW LEVEL SECURITY;
ALTER TABLE public.active_thread_selection ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.active_thread_selection FORCE ROW LEVEL SECURITY;
ALTER TABLE trusted_web.response_transcript_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE trusted_web.response_transcript_v1 FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_integrity.assistant_transcript_attestation_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_integrity.assistant_transcript_attestation_v1 FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.memory_ingest_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_ingest_private.memory_ingest_outbox FORCE ROW LEVEL SECURITY;

CREATE POLICY threads_owner_isolation ON public.threads
  FOR ALL TO sage
  USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY chat_log_owner_isolation ON public.chat_log
  FOR ALL TO sage
  USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY chat_attachments_owner_isolation ON public.chat_attachments
  FOR ALL TO sage
  USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY active_thread_selection_owner_isolation
  ON public.active_thread_selection
  FOR ALL TO sage
  USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY owner_internal ON memory_ingest_private.memory_ingest_outbox
  FOR ALL TO sage USING (true) WITH CHECK (true);

RESET ROLE;
