BEGIN;

CREATE SCHEMA IF NOT EXISTS user_settings AUTHORIZATION sage;

CREATE TABLE user_settings.assistant_response_preference_v1 (
  owner_user_id uuid PRIMARY KEY,
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  assistant_name text,
  nickname text,
  occupation text,
  more_about_you text,
  custom_instructions text,
  response_length text NOT NULL DEFAULT 'balanced'
    CHECK (response_length IN ('concise','balanced','detailed')),
  technical_depth text NOT NULL DEFAULT 'balanced'
    CHECK (technical_depth IN ('plain','balanced','expert')),
  response_format text NOT NULL DEFAULT 'auto'
    CHECK (response_format IN ('auto','prose','bullets','steps')),
  conversation_style text NOT NULL DEFAULT 'natural'
    CHECK (conversation_style IN ('direct','natural','warm')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (assistant_name IS NULL OR char_length(assistant_name) BETWEEN 1 AND 40),
  CHECK (nickname IS NULL OR char_length(nickname) BETWEEN 1 AND 64),
  CHECK (occupation IS NULL OR char_length(occupation) BETWEEN 1 AND 160),
  CHECK (more_about_you IS NULL OR char_length(more_about_you) BETWEEN 1 AND 2000),
  CHECK (
    custom_instructions IS NULL
    OR char_length(custom_instructions) BETWEEN 1 AND 1200
  )
);

ALTER TABLE user_settings.assistant_response_preference_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings.assistant_response_preference_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY assistant_response_preference_owner_policy
  ON user_settings.assistant_response_preference_v1
  USING (
    owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid
  );

ALTER TABLE user_settings.assistant_response_preference_v1 OWNER TO sage;
REVOKE ALL ON SCHEMA user_settings FROM PUBLIC;
REVOKE ALL ON user_settings.assistant_response_preference_v1
  FROM PUBLIC,brains_app;
GRANT USAGE ON SCHEMA user_settings TO brains_app;
GRANT SELECT,INSERT,UPDATE
  ON user_settings.assistant_response_preference_v1
  TO brains_app;

COMMENT ON TABLE user_settings.assistant_response_preference_v1 IS
  'Explicit owner-scoped AI response preferences; separate from governed memory.';

COMMIT;
