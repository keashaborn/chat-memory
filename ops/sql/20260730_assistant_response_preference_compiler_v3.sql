BEGIN;

LOCK TABLE user_settings.assistant_response_preference_v1
  IN SHARE ROW EXCLUSIVE MODE;

LOCK TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_ids_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_summary_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_compiler_version_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_count_v3,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_ids_v3,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_summary_count_v3,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_compiler_version_v3;

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  ADD CONSTRAINT assistant_preference_candidate_rule_count_v3
    CHECK (cardinality(rule_ids) <= 12),
  ADD CONSTRAINT assistant_preference_candidate_rule_ids_v3
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
        'candid_uncertainty',
        'contextual_playfulness',
        'precise_plain_language',
        'evidence_first_conclusions',
        'information_dense',
        'calm_patient_tone',
        'contextual_poetic_language'
      ]::text[]
    ),
  ADD CONSTRAINT assistant_preference_candidate_summary_count_v3
    CHECK (cardinality(summary) <= 16),
  ADD CONSTRAINT assistant_preference_candidate_compiler_version_v3
    CHECK (
      compiler_version IN (
        'assistant_preference_compiler_v1',
        'assistant_preference_compiler_v2',
        'assistant_preference_compiler_v3'
      )
    );

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler_v2,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler_v3;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_active_compiler_v3
    CHECK (
      active_compiler_version IS NULL
      OR active_compiler_version IN (
        'assistant_preference_compiler_v1',
        'assistant_preference_compiler_v2',
        'assistant_preference_compiler_v3'
      )
    );

COMMIT;
