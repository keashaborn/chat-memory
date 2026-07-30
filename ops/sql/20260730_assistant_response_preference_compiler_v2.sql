BEGIN;

LOCK TABLE user_settings.assistant_response_preference_v1
  IN SHARE ROW EXCLUSIVE MODE;

LOCK TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_narrative_length;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_narrative_length
  CHECK (
    preference_narrative IS NULL
    OR char_length(preference_narrative) BETWEEN 1 AND 8000
  );

COMMENT ON COLUMN
  user_settings.assistant_response_preference_v1.preference_narrative IS
  'Owner-authored editable preference narrative, maximum 8000 characters. '
  'Never projected directly into an answer prompt; only an approved typed '
  'compilation may affect response presentation.';

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilatio_source_narrative_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candid_rule_ids_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candi_rule_ids_check1,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candida_summary_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilatio_compiler_version_check,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_narrative_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_ids_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_summary_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_compiler_version_v2;

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  ADD CONSTRAINT assistant_preference_candidate_narrative_v2
    CHECK (char_length(source_narrative) <= 8000),
  ADD CONSTRAINT assistant_preference_candidate_rule_count_v2
    CHECK (cardinality(rule_ids) <= 8),
  ADD CONSTRAINT assistant_preference_candidate_rule_ids_v2
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
        'information_dense'
      ]::text[]
    ),
  ADD CONSTRAINT assistant_preference_candidate_summary_count_v2
    CHECK (cardinality(summary) <= 12),
  ADD CONSTRAINT assistant_preference_candidate_compiler_version_v2
    CHECK (
      compiler_version IN (
        'assistant_preference_compiler_v1',
        'assistant_preference_compiler_v2'
      )
    );

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler_v2;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_active_compiler_v2
    CHECK (
      active_compiler_version IS NULL
      OR active_compiler_version IN (
        'assistant_preference_compiler_v1',
        'assistant_preference_compiler_v2'
      )
    );

COMMIT;
