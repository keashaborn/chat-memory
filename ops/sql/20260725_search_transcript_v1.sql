BEGIN;

CREATE TABLE trusted_web.response_transcript_v1 (
  response_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  user_chat_log_id uuid NOT NULL UNIQUE
    REFERENCES public.chat_log(id) ON DELETE CASCADE,
  assistant_chat_log_id uuid NOT NULL UNIQUE
    REFERENCES public.chat_log(id) ON DELETE CASCADE,
  request_id text NOT NULL
    CHECK (request_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'),
  search_id uuid NOT NULL,
  route text NOT NULL
    CHECK (route IN ('trusted_health','current_news')),
  policy_version text NOT NULL,
  decision text NOT NULL
    CHECK (decision IN ('indexed','live')),
  query_sha256 text NOT NULL
    CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
  answer_sha256 text NOT NULL
    CHECK (answer_sha256 ~ '^[0-9a-f]{64}$'),
  cited_sources jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(cited_sources) = 'array'),
  admitted_sources jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(admitted_sources) = 'array'),
  consulted_source_count integer NOT NULL DEFAULT 0
    CHECK (consulted_source_count BETWEEN 0 AND 50),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,thread_id,request_id)
);

CREATE INDEX response_transcript_owner_thread_created_idx
  ON trusted_web.response_transcript_v1(
    owner_user_id,thread_id,created_at DESC,response_id DESC
  );

ALTER TABLE trusted_web.response_transcript_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE trusted_web.response_transcript_v1 FORCE ROW LEVEL SECURITY;

CREATE POLICY response_transcript_owner_policy
  ON trusted_web.response_transcript_v1
  USING (
    owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  )
  WITH CHECK (
    owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
  );

ALTER TABLE trusted_web.response_transcript_v1 OWNER TO brains_app;
REVOKE ALL ON trusted_web.response_transcript_v1 FROM PUBLIC;

COMMENT ON TABLE trusted_web.response_transcript_v1 IS
  'Owner-scoped, conversation-visible web answers; excluded from Qdrant and long-term memory extraction.';

COMMIT;
