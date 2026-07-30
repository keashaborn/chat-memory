BEGIN;

CREATE TABLE user_settings.assistant_response_preference_compilation_candidate_v1 (
  candidate_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  source_revision bigint NOT NULL CHECK (source_revision >= 0),
  source_narrative text NOT NULL
    CHECK (char_length(source_narrative) <= 1200),
  source_narrative_sha256 text NOT NULL
    CHECK (source_narrative_sha256 ~ '^[0-9a-f]{64}$'),
  proposed_response_length text
    CHECK (
      proposed_response_length IS NULL
      OR proposed_response_length IN ('concise','balanced','detailed')
    ),
  proposed_technical_depth text
    CHECK (
      proposed_technical_depth IS NULL
      OR proposed_technical_depth IN ('plain','balanced','expert')
    ),
  proposed_response_format text
    CHECK (
      proposed_response_format IS NULL
      OR proposed_response_format IN ('auto','prose','bullets','steps')
    ),
  proposed_conversation_style text
    CHECK (
      proposed_conversation_style IS NULL
      OR proposed_conversation_style IN ('direct','natural','warm')
    ),
  rule_ids text[] NOT NULL DEFAULT ARRAY[]::text[]
    CHECK (cardinality(rule_ids) <= 6)
    CHECK (
      rule_ids <@ ARRAY[
        'direct_answers_first',
        'restrained_reassurance',
        'evidence_based_challenge',
        'no_unsolicited_closing_offers',
        'minimal_paraphrase',
        'no_generic_praise',
        'practical_focus',
        'question_restraint',
        'candid_uncertainty'
      ]::text[]
    ),
  rejected_reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[]
    CHECK (cardinality(rejected_reason_codes) <= 8)
    CHECK (
      rejected_reason_codes <@ ARRAY[
        'cannot_change_safety',
        'cannot_change_factual_standards',
        'cannot_force_agreement',
        'cannot_expose_hidden_prompts',
        'cannot_override_domain_policy',
        'cannot_control_tools_or_memory',
        'unsupported_style_request',
        'ambiguous_request'
      ]::text[]
    ),
  compilation_status text NOT NULL
    CHECK (compilation_status IN ('accepted','partial','rejected','clear')),
  summary text[] NOT NULL DEFAULT ARRAY[]::text[]
    CHECK (cardinality(summary) <= 10),
  not_applied text[] NOT NULL DEFAULT ARRAY[]::text[]
    CHECK (cardinality(not_applied) <= 8),
  compiler_version text NOT NULL
    CHECK (compiler_version = 'assistant_preference_compiler_v1'),
  plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
  provider_model text NOT NULL CHECK (char_length(provider_model) BETWEEN 1 AND 120),
  provider_response_id text
    CHECK (
      provider_response_id IS NULL
      OR char_length(provider_response_id) BETWEEN 1 AND 255
    ),
  status text NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','approved','superseded')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  approved_at timestamptz,
  UNIQUE (owner_user_id,candidate_id),
  CHECK (expires_at > created_at),
  CHECK (
    (status = 'approved' AND approved_at IS NOT NULL)
    OR (status <> 'approved' AND approved_at IS NULL)
  )
);

CREATE INDEX assistant_response_preference_compilation_owner_status_idx
  ON user_settings.assistant_response_preference_compilation_candidate_v1
  (owner_user_id,status,created_at DESC);

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY assistant_response_preference_compilation_owner_policy
  ON user_settings.assistant_response_preference_compilation_candidate_v1
  USING (
    owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid
  )
  WITH CHECK (
    owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid
  );

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD COLUMN preference_narrative text,
  ADD COLUMN active_compilation_candidate_id uuid,
  ADD COLUMN active_compilation_summary text[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN active_compilation_rejections text[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN active_compilation_plan_sha256 text,
  ADD COLUMN active_compiler_version text,
  ADD COLUMN active_compiled_at timestamptz;

UPDATE user_settings.assistant_response_preference_v1
   SET preference_narrative=custom_instructions,
       custom_instructions=NULL
 WHERE custom_instructions IS NOT NULL;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_narrative_length
    CHECK (
      preference_narrative IS NULL
      OR char_length(preference_narrative) BETWEEN 1 AND 1200
    ),
  ADD CONSTRAINT assistant_response_preference_compiled_marker
    CHECK (
      custom_instructions IS NULL
      OR custom_instructions LIKE 'assistant-preference-plan-v1:%'
    ),
  ADD CONSTRAINT assistant_response_preference_active_plan_sha
    CHECK (
      active_compilation_plan_sha256 IS NULL
      OR active_compilation_plan_sha256 ~ '^[0-9a-f]{64}$'
    ),
  ADD CONSTRAINT assistant_response_preference_active_compiler
    CHECK (
      active_compiler_version IS NULL
      OR active_compiler_version = 'assistant_preference_compiler_v1'
    ),
  ADD CONSTRAINT assistant_response_preference_active_candidate_fk
    FOREIGN KEY (owner_user_id,active_compilation_candidate_id)
    REFERENCES user_settings.assistant_response_preference_compilation_candidate_v1
    (owner_user_id,candidate_id);

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  OWNER TO sage;
REVOKE ALL
  ON user_settings.assistant_response_preference_compilation_candidate_v1
  FROM PUBLIC,brains_app;
GRANT SELECT,INSERT,UPDATE
  ON user_settings.assistant_response_preference_compilation_candidate_v1
  TO brains_app;

COMMENT ON COLUMN
  user_settings.assistant_response_preference_v1.preference_narrative IS
  'Editable owner prose; never projected directly into an answer prompt.';
COMMENT ON COLUMN
  user_settings.assistant_response_preference_v1.custom_instructions IS
  'Server-generated marker containing only approved response-rule identifiers.';
COMMENT ON TABLE
  user_settings.assistant_response_preference_compilation_candidate_v1 IS
  'Owner-scoped review candidates for typed assistant response preferences.';

COMMIT;
